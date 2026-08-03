# Issue #62 isolated live-evidence sidecar design

## Scope

Add a standalone, standard-library-only capture sidecar for a separately
authorized Issue #62 live-control window. The sidecar is evidence tooling, not
Gateway code. It must not be imported or started by Desktop, Gateway, normal
builds, or debug builds.

Synthetic tests prove only `synthetic_lane_verified`. They do not update the
runtime inventory, satisfy a live gate, qualify a model, close Issue #62, or
unlock a downstream issue.

## Activation and topology

The script refuses to listen unless `--enable-live-capture` is present. Its
listen address must resolve to IPv4 or IPv6 loopback. Every instance has one
declared hop (`pre` or `post`), one forward base URL, one isolated output
directory, and one pre-existing HMAC key file.

An authorized run starts two independent processes:

```text
isolated client -> pre sidecar -> isolated Gateway -> post sidecar -> upstream
```

Only temporary, isolated client/Gateway configuration points at the sidecars.
No production route, telemetry schema, diagnostic recorder, shared config, or
running Desktop/Gateway process is changed.

## Capture contract

For each request, a sidecar forwards the exact application body bytes while
incrementally computing:

- full request-body byte count, SHA-256, and keyed HMAC-SHA-256;
- full response-body byte count, SHA-256, and keyed HMAC-SHA-256; and
- for an SSE response, an order-sensitive digest over length-prefixed complete
  SSE frames, plus frame count, byte count, and allow-listed terminal classes.

The HMAC domains are stable across the two hops so equal bytes produce equal
digests. HTTP hop-by-hop framing is intentionally excluded: fingerprints cover
the decoded HTTP message body bytes delivered to each application boundary.

The final JSON record contains only the schema, hop, opaque capture id,
completion outcome, bounded failure code, status, coarse content-type class,
byte counts, digests, and bounded SSE classifications. It never stores or
prints a target URL, path, header, credential, HMAC key, raw body, prompt,
tool argument/result, or wire identifier.

## Bounds and failure behavior

The operator must provide positive request/response byte caps and connect,
read, and overall timeouts. Invalid content length, request or response
overflow, upstream timeout, downstream cancellation, forwarding failure, and
incomplete SSE framing produce a fail-closed `incomplete` record. An incomplete
record can describe the bounded failure but can never set
`synthetic_lane_verified=true`.

Each record is written to a unique `.partial` file, flushed, and atomically
renamed only after serialization succeeds. Every error path removes its
`.partial` file. Shutdown stops accepting work, closes the listening socket,
and leaves no partial artifact. No raw-body spool file exists.

## Tests

Focused tests use only real loopback HTTP servers and synthetic fixtures. They
cover:

1. two sidecars produce matching, independently written full request and
   response SHA/HMAC values for exact passthrough bytes;
2. SSE chunk boundaries do not affect the ordered sequence digest;
3. activation and loopback checks reject default or unsafe startup;
4. request/response overflow and timeouts fail closed;
5. prompts, credentials, paths, raw SSE data, and key material never appear in
   artifacts; and
6. success, failure, cancellation, and shutdown leave no `.partial` files or
   listening sidecar threads.

The test fixture summary may say `synthetic_lane_verified`; no test or script
writes Issue #62 inventory qualification fields.
