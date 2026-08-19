use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

#[derive(Debug, Clone)]
pub enum AppServerPoll {
    Message(Value),
    Timeout,
    Closed,
}

pub struct AppServerCall {
    pub id: Value,
    pub method: &'static str,
    pub params: Option<Value>,
}

pub struct AppServerSession {
    child: Child,
    stdin: Option<ChildStdin>,
    receiver: mpsc::Receiver<Result<Option<String>, String>>,
    killed: bool,
    #[cfg(windows)]
    _job: AppServerJob,
}

impl AppServerSession {
    pub fn start(mut command: Command, purpose: &str) -> Result<Self, String> {
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        configure_no_window(&mut command);
        let mut child = command
            .spawn()
            .map_err(|error| format!("failed to start codex app-server for {purpose}: {error}"))?;
        #[cfg(windows)]
        let job = match AppServerJob::assign_to(&child) {
            Ok(job) => job,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| {
                let _ = child.kill();
                let _ = child.wait();
                "failed to open codex app-server stdin".to_string()
            })?;
        let stdout = child.stdout.take().ok_or_else(|| {
            let _ = child.kill();
            let _ = child.wait();
            "failed to open codex app-server stdout".to_string()
        })?;
        Ok(Self {
            child,
            stdin: Some(stdin),
            receiver: spawn_line_reader(stdout),
            killed: false,
            #[cfg(windows)]
            _job: job,
        })
    }

    pub fn initialize(&mut self) -> Result<(), String> {
        self.write(&json!({
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "codexhub",
                    "title": "CodexHub",
                    "version": env!("CARGO_PKG_VERSION")
                },
                "capabilities": {
                    "experimentalApi": true,
                    "requestAttestation": false,
                    "optOutNotificationMethods": []
                }
            }
        }))?;
        self.write(&json!({ "method": "initialized" }))
    }

    pub fn send_calls(&mut self, calls: &[AppServerCall], purpose: &str) -> Result<(), String> {
        for call in calls {
            let mut payload = json!({
                "id": call.id,
                "method": call.method,
            });
            if let Some(params) = &call.params {
                payload["params"] = params.clone();
            }
            self.write(&payload)?;
        }
        self.flush(purpose)
    }

    pub fn poll_message(&mut self, remaining: Duration) -> Result<AppServerPoll, String> {
        loop {
        match self.receiver.recv_timeout(remaining) {
            Ok(Ok(Some(line))) => {
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    continue;
                }
                match serde_json::from_str::<Value>(trimmed) {
                    Ok(message) => return Ok(AppServerPoll::Message(message)),
                    Err(_) => continue,
                }
            }
            Ok(Ok(None)) => return Ok(AppServerPoll::Closed),
            Ok(Err(error)) => return Err(error),
            Err(mpsc::RecvTimeoutError::Timeout) => return Ok(AppServerPoll::Timeout),
            Err(mpsc::RecvTimeoutError::Disconnected) => return Ok(AppServerPoll::Closed),
        }
        }
    }

    pub fn request(
        &mut self,
        calls: &[AppServerCall],
        timeout: Duration,
        purpose: &str,
    ) -> Result<Vec<Value>, String> {
        self.send_calls(calls, purpose)?;
        let mut pending: HashMap<String, Value> = calls
            .iter()
            .map(|call| (call.id.to_string(), call.id.clone()))
            .collect();
        let mut results: HashMap<String, Value> = HashMap::new();
        let deadline = Instant::now() + timeout;
        while !pending.is_empty() {
            let message = self.read_message(deadline, timeout, purpose)?;
            if let Some(id) = message.get("id") {
                let key = id.to_string();
                if pending.remove(&key).is_some() {
                    results.insert(key, message);
                }
            }
        }
        Ok(calls
            .iter()
            .map(|call| results.remove(&call.id.to_string()).expect("collected"))
            .collect())
    }

    pub fn kill(&mut self) {
        if self.killed {
            return;
        }
        self.killed = true;
        let _ = self.child.kill();
        let _ = self.child.wait();
    }

    fn write(&mut self, value: &Value) -> Result<(), String> {
        let stdin = self
            .stdin
            .as_mut()
            .ok_or_else(|| "codex app-server stdin already closed".to_string())?;
        serde_json::to_writer(&mut *stdin, value)
            .map_err(|error| format!("failed to encode codex app-server request: {error}"))?;
        stdin
            .write_all(b"\n")
            .map_err(|error| format!("failed to write codex app-server request: {error}"))
    }

    fn flush(&mut self, purpose: &str) -> Result<(), String> {
        let stdin = self
            .stdin
            .as_mut()
            .ok_or_else(|| "codex app-server stdin already closed".to_string())?;
        stdin
            .flush()
            .map_err(|error| format!("failed to flush codex app-server {purpose} request: {error}"))
    }

    fn read_message(&mut self, deadline: Instant, timeout: Duration, purpose: &str) -> Result<Value, String> {
        loop {
            let now = Instant::now();
            if now >= deadline {
                self.kill();
                return Err(format!(
                    "codex app-server {purpose} timed out after {} seconds",
                    timeout.as_secs()
                ));
            }
            let remaining = deadline.saturating_duration_since(now);
            match self.receiver.recv_timeout(remaining) {
                Ok(Ok(Some(line))) => {
                    let trimmed = line.trim();
                    if trimmed.is_empty() {
                        continue;
                    }
                    match serde_json::from_str::<Value>(trimmed) {
                        Ok(message) => return Ok(message),
                        Err(_) => continue,
                    }
                }
                Ok(Ok(None)) => {
                    let _ = self.child.wait();
                    return Err(format!(
                        "codex app-server {purpose} did not return a response"
                    ));
                }
                Ok(Err(error)) => {
                    self.kill();
                    return Err(format!(
                        "failed to read codex app-server {purpose} response: {error}"
                    ));
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    self.kill();
                    return Err(format!(
                        "codex app-server {purpose} timed out after {} seconds",
                        timeout.as_secs()
                    ));
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    let _ = self.child.wait();
                    return Err(format!(
                        "codex app-server {purpose} reader stopped before a response"
                    ));
                }
            }
        }
    }
}

impl Drop for AppServerSession {
    fn drop(&mut self) {
        self.kill();
    }
}

fn spawn_line_reader(
    stdout: ChildStdout,
) -> mpsc::Receiver<Result<Option<String>, String>> {
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) => {
                    let _ = sender.send(Ok(None));
                    break;
                }
                Ok(_) => {
                    if sender.send(Ok(Some(line.clone()))).is_err() {
                        break;
                    }
                }
                Err(error) => {
                    let _ = sender.send(Err(error.to_string()));
                    break;
                }
            }
        }
    });
    receiver
}

fn configure_no_window(command: &mut Command) {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = command;
    }
}

#[cfg(windows)]
struct AppServerJob(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
impl AppServerJob {
    fn assign_to(child: &Child) -> Result<Self, String> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err("Failed to prepare codex app-server cleanup job.".to_string());
        }
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                std::ptr::addr_of!(limits).cast(),
                std::mem::size_of_val(&limits) as u32,
            )
        };
        if configured == 0 {
            unsafe { windows_sys::Win32::Foundation::CloseHandle(handle) };
            return Err("Failed to prepare codex app-server cleanup job.".to_string());
        }
        let assigned = unsafe { AssignProcessToJobObject(handle, child.as_raw_handle() as _) };
        if assigned == 0 {
            unsafe { windows_sys::Win32::Foundation::CloseHandle(handle) };
            return Err("Failed to bind codex app-server cleanup job.".to_string());
        }
        Ok(Self(handle))
    }
}

#[cfg(windows)]
impl Drop for AppServerJob {
    fn drop(&mut self) {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.0) };
    }
}
