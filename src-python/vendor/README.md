# Vendored Python transport dependency

CodexHub's embeddable Python runtime does not enable `site-packages`. The Gateway therefore loads the pinned pure-Python wheel in this directory directly from `sys.path`.

- Package: `urllib3`
- Version: `2.7.0`
- Source: `https://pypi.org/project/urllib3/2.7.0/`
- Wheel: `urllib3-2.7.0-py3-none-any.whl`
- SHA-256: `9fb4c81ebbb1ce9531cce37674bbc6f1360472bc18ca9a553ede278ef7276897`
- License: MIT; the wheel contains its upstream license metadata.

The dependency is limited to the Official upstream transport. Third-party Provider routing continues to use its existing transport path.
