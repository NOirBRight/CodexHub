# Vendored Python transport dependency

CodexHub's embeddable Python runtime does not enable `site-packages`. The Gateway therefore loads the pinned pure-Python wheel in this directory directly from `sys.path`.

- Package: `urllib3`
- Version: `2.7.0`
- Source: `https://pypi.org/project/urllib3/2.7.0/`
- Wheel: `urllib3-2.7.0-py3-none-any.whl`
- SHA-256: `9fb4c81ebbb1ce9531cce37674bbc6f1360472bc18ca9a553ede278ef7276897`
- License: MIT; the wheel contains its upstream license metadata.

The dependency is used by the Gateway HTTP transport.

Cursor AgentService uses pinned pure Python HTTP/2 wheels, loaded by
`subscription_backend_contract.load_http2_dependencies`. The existing Tauri
wheel resource glob includes them on Linux and Windows; no runtime pip install
or site-packages is required.

| Package | Version | Wheel SHA-256 | Source |
| --- | --- | --- | --- |
| h2 | 4.3.0 | c438f029a25f7945c69e0ccf0fb951dc3f73a5f6412981daee861431b70e2bdd | https://pypi.org/project/h2/4.3.0/ |
| hpack | 4.1.0 | 157ac792668d995c657d93111f46b4535ed114f0c9c8d672271bbec7eae1b496 | https://pypi.org/project/hpack/4.1.0/ |
| hyperframe | 6.1.0 | b03380493a519fce58ea5af42e4a42317bf9bd425596f7a0835ffce80f1a42e5 | https://pypi.org/project/hyperframe/6.1.0/ |

All three wheels include their upstream MIT license metadata.
