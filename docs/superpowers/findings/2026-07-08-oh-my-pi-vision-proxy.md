# oh-my-pi Vision Proxy Notes

Date: 2026-07-08

Source inspected:
- Repository: https://github.com/can1357/oh-my-pi
- Local clone HEAD: `ac2ea80fa306bcb91ec95ac0ab24949394879015`

## Summary

oh-my-pi's image handling is not primarily an HTTP proxy rewrite. It is an agent/session-layer fallback:

1. The UI/session keeps pasted images as first-class `ImageContent`.
2. If the active model is text-only and `images.describeForTextModels` is enabled, the session builds a hidden custom message before the user message.
3. Each image is saved under the session `local://` root, then a vision-capable model describes it.
4. The text-only model receives an injected text block shaped like `<image path="local://...">description</image>`.
5. Provider adapters still have a final guard that omits unsupported images with a placeholder instead of allowing raw image payloads to reach a text-only model.

## Key Implementation Points

- `packages/coding-agent/src/utils/image-vision-fallback.ts` owns the fallback. Its file comment says the feature saves each image under `local://`, asks a vision model to describe it, and injects the result as text for models that cannot accept images.
- `imageFileName()` uses content-addressed names based on `Bun.hash(image.data)`, so repeated image bytes reuse the same artifact.
- `saveImage()` writes the base64 image under the local root and returns a `local://...` URL.
- `resolveVisionModel()` mirrors the `inspect_image` priority: `pi/vision`, then `pi/default`, then the active model, then the first available image-capable model. It filters candidates with `model.input.includes("image")`.
- `describeImage()` calls `instrumentedCompleteSimple()` with a special telemetry kind, `image_attachment_describe`, and a dedicated system/user prompt pair.
- `describeAttachedImagesForTextModel()` never throws for individual image description failures. If no vision model/API key is available, or the vision call returns no usable text, it still emits an `<image>` text block containing an explanatory fallback note.
- `packages/coding-agent/src/session/agent-session.ts` calls `#buildImageDescriptionNotice()` when the active model is text-only, `images.blockImages` is false, and `images.describeForTextModels` is true. The resulting custom message has `display: false`, so the model sees it but the UI does not render it as a normal user message.
- `packages/ai/src/providers/vision-guard.ts` and provider adapters add defense in depth. If a payload still contains images for a non-vision model, image blocks are omitted and a placeholder is inserted.
- `packages/coding-agent/src/session/provider-image-budget.ts` clamps older image blocks for actual vision models according to the provider image budget, replacing emptied tool-result image content with `[image omitted: provider image limit]`.
- `packages/coding-agent/src/prompts/tools/image-attachment-describe*.md` is stronger than a generic image-caption prompt: it explicitly asks for OCR, UI state, controls, errors, chart/table structure, ambiguity marking, and evidence-first wording.

## What CodexHub Can Reuse

- Treat image proxying as a model-capability fallback, not as a separate "transparent proxy" feature in the user-facing mental model.
- Keep one user-facing image setting. A hidden transparent overlay gate should default to the global image proxy setting, which matches the fix already made in CodexHub.
- Add a final provider-boundary guard: if the target model is known text-only, raw `input_image`/`image_url` should not be forwarded. Either describe it, strip with a visible placeholder, or fail before the provider call with a CodexHub-owned error.
- Improve the image-description prompt using oh-my-pi's emphasis on OCR, UI labels/states, charts/tables, uncertainty, and compact factual wording.
- Consider storing image artifacts content-addressed with TTL and injecting stable `<image path="codexhub://...">description</image>` style text. For CodexHub this should be an internal/debug reference, because external clients like ZCode cannot resolve `local://`.
- Consider a graceful-degradation mode: if the vision subrequest fails, the main request can continue with an explanatory image-unavailable note. This should likely be configurable because silent image loss can mislead the user.
- Add provider image-count clamping for requests that really do go to image-capable target models.

## What Not To Copy Directly

oh-my-pi owns the editor, pending-image queue, session persistence, `local://` resolver, and tool layer. CodexHub is an HTTP gateway serving external clients such as ZCode, so it usually sees only OpenAI-compatible wire JSON after the client has already built the request. The agent/session parts are useful as architecture guidance, but a direct port would not fit CodexHub's boundary.

For CodexHub, the practical equivalent is a "Vision Overlay v2" at the gateway preprocessing/provider-boundary layer:

1. Detect target model vision capability.
2. Normalize all inbound OpenAI Responses and Chat Completions image parts into internal image records.
3. Resolve a vision model role or configured fallback.
4. Describe images and replace them with explicit `<image>` text blocks before forwarding to text-only targets.
5. Add a final invariant check that text-only upstream payloads contain no raw image parts.
6. Log/cache artifact hash, vision model, cache hit, and fallback reason.
