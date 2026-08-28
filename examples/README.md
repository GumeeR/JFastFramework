# Examples

| Example | Shows |
| --- | --- |
| `hello/` | Minimal service: `create_app`, one router, problem+json errors, system endpoints |

Run one:

```bash
cd hello
pip install -e "../..[server,metrics,dev]"
uvicorn main:app --reload
```
