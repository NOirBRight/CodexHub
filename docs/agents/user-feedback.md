# User feedback

Apply these rules to user-initiated changes in persistent settings, routing, ownership, and runtime state.

1. Every user-initiated persistent state change must end with a success or error Toast. Do not leave completion implicit in the changed control.
2. If the change has a sustained process, create one persistent loading Toast before work starts, then update that same Toast to the completed success or error state. Do not create a second completion Toast.
3. If the change is not immediately effective, the completion Toast must name the exact client or runtime the user must restart. Do not use vague phrases such as “restart if needed.”

Clipboard copy feedback and other momentary, non-persistent interactions may use an inline acknowledgement instead of a Toast.
