import asyncio
import secrets
import sys
import time

from settings import AMD_DIR, amd_config

sys.path.insert(0, str(AMD_DIR))

from google.protobuf import empty_pb2  # noqa: E402
from grpc import ssl_channel_credentials  # noqa: E402
from grpc.aio import EOF, insecure_channel, secure_channel  # noqa: E402
from src.grpc.manager_pb2 import LoginData, LoginRequest  # noqa: E402
from src.grpc.manager_pb2_grpc import WrapperManagerServiceStub  # noqa: E402

_cache: tuple[float, dict] | None = None


def _channel():
    inst = amd_config().get("instance", {})
    url, secure = inst.get("url", "127.0.0.1:8080"), bool(inst.get("secure"))
    return url, (secure_channel(url, ssl_channel_credentials()) if secure else insecure_channel(url))


async def status(fresh: bool = False) -> dict:
    global _cache
    if not fresh and _cache and time.time() - _cache[0] < 15:
        return _cache[1]
    url, channel = _channel()
    try:
        resp = await WrapperManagerServiceStub(channel).Status(empty_pb2.Empty(), timeout=5)
        if resp.header.code != 0:
            out = {"ok": False, "url": url, "error": resp.header.msg}
        else:
            d = resp.data
            out = {"ok": bool(d.regions), "url": url, "ready": d.ready, "regions": list(d.regions),
                   "clients": d.client_count,
                   "error": None if d.regions else "No Apple Music account logged in to wrapper-manager"}
    except Exception as e:
        out = {"ok": False, "url": url, "error": f"Unreachable: {getattr(e, 'details', lambda: str(e))()}"}
    finally:
        await channel.close()
    _cache = (time.time(), out)
    return out


class LoginFlow:
    """One Apple Music account login against wrapper-manager (bidi gRPC stream, may ask for 2FA)."""

    def __init__(self, username: str, password: str):
        self.id = secrets.token_urlsafe(12)
        self.username, self._password = username, password
        self.state, self.message = "pending", "Starting wrapper…"
        self.created = time.time()
        self._changed = asyncio.Event()
        self._queue: asyncio.Queue = asyncio.Queue()
        _, self._channel = _channel()
        self._task = asyncio.create_task(self._run())

    async def _requests(self):
        while (item := await self._queue.get()) is not None:
            yield item

    def _set(self, state: str, message: str):
        global _cache
        if state in ("ok", "error"):
            _cache = None
        self.state, self.message = state, message
        self._changed.set()

    async def _run(self):
        call = WrapperManagerServiceStub(self._channel).Login(self._requests())
        await self._queue.put(LoginRequest(data=LoginData(username=self.username, password=self._password)))
        try:
            while True:
                reply = await asyncio.wait_for(call.read(), timeout=300)
                if reply is EOF:
                    self._set("error", "wrapper-manager closed the login stream")
                    break
                code, msg = reply.header.code, reply.header.msg
                if code == 2:
                    self._set("2fa", "Enter the two-factor code sent to your Apple devices")
                    continue
                if code == 0:
                    self._set("ok", "Logged in")
                else:
                    self._set("error", msg or "Login failed")
                break
        except asyncio.TimeoutError:
            self._set("error", "Timed out waiting for wrapper-manager")
        except Exception as e:
            self._set("error", f"Login failed: {getattr(e, 'details', lambda: str(e))()}")
        finally:
            self._password = ""
            await self._queue.put(None)
            call.cancel()
            await self._channel.close()
            global _cache
            _cache = None

    async def submit_2fa(self, code: str):
        if self.state != "2fa":
            raise ValueError("Not waiting for a 2FA code")
        self._set("pending", "Verifying code…")
        await self._queue.put(LoginRequest(data=LoginData(username=self.username, password=self._password,
                                                          two_step_code=code)))

    async def wait(self, timeout: float) -> dict:
        self._changed.clear()
        if self.state == "pending":
            try:
                await asyncio.wait_for(self._changed.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        return {"flow": self.id, "state": self.state, "message": self.message}


_flows: dict[str, LoginFlow] = {}


def start_login(username: str, password: str) -> LoginFlow:
    for fid, f in list(_flows.items()):
        if time.time() - f.created > 900:
            _flows.pop(fid, None)
    flow = LoginFlow(username, password)
    _flows[flow.id] = flow
    return flow


def get_flow(fid: str) -> LoginFlow | None:
    return _flows.get(fid)
