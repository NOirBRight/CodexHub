# 日常浏览器授权连接（测试版）

适用于 Chromium / Chrome / Edge。将本目录以“加载已解压的扩展”方式安装到你日常使用的浏览器资料中；不另建浏览器资料。

1. 在日常浏览器打开 ChatGPT，以已有 Google 登录或 Passkey 完成认证。
2. 从 CodexHub 打开 ChatGPT Runtime Settings，确保它也在同一个浏览器资料中。
3. 点击扩展图标 →“连接此浏览器的 ChatGPT 账号”。
4. 本机组件验证会话后保存，提示你手动重启 ChatGPT 组件。保存不会重启组件或打断进行中的请求。

权限：`cookies` 仅获准访问 `https://chatgpt.com/*`；`activeTab` 和 `scripting` 只在你点击扩展时用于当前本机设置页。没有后台采集、远程上传、Google 站点权限、浏览器密码或 Passkey 读取功能。
扩展不保存会话副本；本机组件将会话存入用户私有目录。浏览器与本机组件不实时同步：会话过期或切换账号后，再次主动连接。

Firefox / Safari 尚未支持。本扩展尚未上架商店，真实浏览器验收完成前不作正式发布。
