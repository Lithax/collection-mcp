"""Keep test runs from writing to the real call log (by default the router logs to <repo>/.collab/router-calls.log).
Tests that want the log point router.CALL_LOG at a temp file themselves."""
import os

os.environ["ROUTER_CALL_LOG"] = "off"
