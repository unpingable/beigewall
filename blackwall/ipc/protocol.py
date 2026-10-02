"""`blackwall-ipc/1`: framing and fail-closed validation.

Framing: 4-byte big-endian length, then that many bytes of UTF-8 JSON
holding exactly one object. Everything from the socket is hostile: the
decoder rejects duplicate keys, non-objects, unknown keys and ops, wrong
types and over-long strings before anything reaches the broker. The server
never evaluates, imports or opens anything named by a client string.
"""

import json
import struct

PROTOCOL = "blackwall-ipc/1"
VERSION = 1
MAX_FRAME = 262144          # bytes, either direction
MAX_ID = 64
MAX_TASK_ID = 128
MAX_STR = 512               # generic string argument bound
MAX_ANSWER = 65536
MAX_SUMMARY = 4096
MAX_PARAMS = 16384          # serialized effect params
MAX_DERIVED = 16
MAX_CLAIMS = 64

REQUEST_KEYS = {"v", "id", "op", "task_id", "args"}

# op -> {arg: (type-or-tuple, required)}
OPS = {
    "hello": {},
    "begin_task": {},
    "request_grant": {"resource": (str, True), "operation": (str, True),
                      "projection": (str, False), "ttl": ((int, float), False),
                      "budget": (dict, False)},
    "read": {"grant_id": (str, True), "resource": (str, True),
             "projection": (str, True)},
    "effect": {"grant_id": (str, True), "effect_name": (str, True),
               "params": (dict, True), "derived_from": (list, True)},
    "discover": {"resource": (str, False)},
    "report_unmediated": {"attempted": (str, True), "args_repr": (str, False)},
    "narrative": {"claims": (list, True), "declared_effects": (list, True),
                  "summary": (str, True)},
    "task_end": {"answer": (str, True), "truncated": (bool, True)},
}


class ProtocolError(Exception):
    def __init__(self, reason: str, close: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.close = close      # framing-level: the connection is dropped


def _no_duplicates(pairs):
    seen = set()
    out = {}
    for k, v in pairs:
        if k in seen:
            raise ProtocolError("duplicate_key")
        seen.add(k)
        out[k] = v
    return out


def decode(payload: bytes) -> dict:
    """Decode one frame body into a validated request dict."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("invalid_utf8", close=True)
    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicates)
    except ProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise ProtocolError("invalid_json", close=True)
    if not isinstance(obj, dict):
        raise ProtocolError("not_an_object", close=True)
    if set(obj) != REQUEST_KEYS:
        raise ProtocolError("bad_keys")
    if obj["v"] is not VERSION and obj["v"] != VERSION or isinstance(obj["v"], bool):
        raise ProtocolError("bad_version")
    for key, limit in (("id", MAX_ID), ("op", MAX_STR), ("task_id", MAX_TASK_ID)):
        if not isinstance(obj[key], str) or not obj[key] or len(obj[key]) > limit:
            raise ProtocolError(f"bad_{key}")
    if not isinstance(obj["args"], dict):
        raise ProtocolError("bad_args")
    spec = OPS.get(obj["op"])
    if spec is None:
        raise ProtocolError("unknown_op")
    args = obj["args"]
    if set(args) - set(spec):
        raise ProtocolError("unknown_arg")
    for name, (typ, required) in spec.items():
        if name not in args:
            if required:
                raise ProtocolError(f"missing_arg:{name}")
            continue
        val = args[name]
        if isinstance(val, bool) and typ is not bool:
            raise ProtocolError(f"bad_type:{name}")
        if not isinstance(val, typ):
            raise ProtocolError(f"bad_type:{name}")
        if isinstance(val, str):
            limit = {"answer": MAX_ANSWER, "summary": MAX_SUMMARY,
                     "args_repr": 200}.get(name, MAX_STR)
            if len(val) > limit:
                raise ProtocolError(f"too_long:{name}")
        if isinstance(val, float) and (val != val or val in (float("inf"), float("-inf"))):
            raise ProtocolError(f"bad_number:{name}")
    if obj["op"] == "request_grant" and "budget" in args:
        b = args["budget"]
        if set(b) != {"reads", "objects"} or not all(
                isinstance(b[k], int) and not isinstance(b[k], bool) for k in b):
            raise ProtocolError("bad_budget")
    if obj["op"] == "effect":
        if len(json.dumps(args["params"], sort_keys=True)) > MAX_PARAMS:
            raise ProtocolError("too_long:params")
        if len(args["derived_from"]) > MAX_DERIVED:
            raise ProtocolError("too_long:derived_from")
        # provenance entries are bounded JSON values, not schema-checked
        # here: a malformed entry must reach the broker and be refused
        # with its own receipt ("malformed derived_from entry"), exactly as
        # in-process, rather than vanish as a protocol rejection
        _strings_only(args["derived_from"], depth=0)
        _strings_only(args["params"], depth=0)
    if obj["op"] == "narrative":
        if len(args["claims"]) > MAX_CLAIMS or len(args["declared_effects"]) > MAX_DERIVED:
            raise ProtocolError("too_long:narrative")
        _strings_only(args["claims"], depth=0)
        _strings_only(args["declared_effects"], depth=0)
    return obj


def _strings_only(value, depth: int) -> None:
    """Params/claims may hold only JSON scalars, lists and dicts of bounded
    depth; nothing else exists in JSON, but depth bombs are refused."""
    if depth > 6:
        raise ProtocolError("too_deep")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str) or len(k) > MAX_STR:
                raise ProtocolError("bad_key")
            _strings_only(v, depth + 1)
    elif isinstance(value, list):
        if len(value) > 256:
            raise ProtocolError("too_long:list")
        for v in value:
            _strings_only(v, depth + 1)
    elif isinstance(value, str):
        if len(value) > MAX_PARAMS:
            raise ProtocolError("too_long:string")
    elif value is None or isinstance(value, (bool, int, float)):
        return
    else:
        raise ProtocolError("bad_value")


def encode_frame(obj: dict) -> bytes:
    body = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ProtocolError("frame_too_large", close=True)
    return struct.pack(">I", len(body)) + body


def read_exact(sock, n: int) -> bytes | None:
    """Read exactly n bytes or return None on EOF."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def read_frame(sock) -> bytes | None:
    """Read one frame body. Returns None on clean EOF. Raises ProtocolError
    (close=True) on an oversized length prefix before reading the body."""
    header = read_exact(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME:
        raise ProtocolError("frame_too_large", close=True)
    if length == 0:
        raise ProtocolError("empty_frame", close=True)
    body = read_exact(sock, length)
    if body is None:
        raise ProtocolError("truncated_frame", close=True)
    return body


def ok_response(req_id: str, result) -> dict:
    return {"v": VERSION, "id": req_id, "ok": True, "result": result}


def error_response(req_id: str | None, code: str, message: str,
                   receipt_id: str | None = None) -> dict:
    err = {"code": code, "message": message}
    if receipt_id:
        err["receipt_id"] = receipt_id
    return {"v": VERSION, "id": req_id or "", "ok": False, "error": err}
