"""Per-request state owned by the Campfire middleware and read by handlers."""

from dataclasses import dataclass, field
from urllib.parse import parse_qsl

from starlette.requests import Request

from .jsonvalue import as_dict, as_list
from .models import Session, User
from .storage import Upload

type CacheVersion = tuple[str, int] | None


@dataclass(slots=True)
class Params:
    """Rails-style parameters: `group[name]` fields, `name[]` lists, uploads or JSON."""

    fields: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    files: dict[str, Upload] = field(default_factory=dict[str, Upload])
    json: dict[str, object] | None = None

    def get(self, name: str, default: str = "") -> str:
        if self.json is not None:
            found = self.json.get(name)
            return found if isinstance(found, str) else default
        for key, found in self.fields:
            if key == name:
                return found
        return default

    def has(self, name: str) -> bool:
        if self.json is not None:
            return name in self.json
        return any(key == name for key, _ in self.fields) or name in self.files

    def getlist(self, name: str) -> list[str]:
        if self.json is not None:
            found = as_list(self.json.get(name.removesuffix("[]")))
            return [str(v) for v in found] if found is not None else []
        return [found for key, found in self.fields if key == name]

    def nested(self, group: str, name: str) -> str | None:
        """`group[name]`, or `{"group": {"name": ...}}` for JSON bodies; None when absent."""
        if self.json is not None:
            inner = as_dict(self.json.get(group))
            found = inner.get(name) if inner is not None else None
            return found if isinstance(found, str) else None
        key = f"{group}[{name}]"
        for candidate, found in self.fields:
            if candidate == key:
                return found
        return None

    def value(self, group: str, name: str, default: str = "") -> str:
        found = self.nested(group, name)
        return default if found is None else found

    def nested_dict(self, group: str, name: str) -> dict[str, object] | None:
        if self.json is None:
            return None
        inner = as_dict(self.json.get(group))
        return as_dict(inner.get(name)) if inner is not None else None

    def file(self, name: str) -> Upload | None:
        return self.files.get(name)


@dataclass(slots=True)
class RequestContext:
    method: str
    scheme: str
    host: str
    path: str
    query_string: str
    remote_addr: str
    headers: dict[str, str]
    cookies: dict[str, str]
    session_data: dict[str, object] = field(default_factory=dict[str, object])
    session_original: dict[str, object] = field(default_factory=dict[str, object])
    current_user: User | None = None
    current_session: Session | None = None
    authenticated_by_bot: bool = False
    cache_version: CacheVersion = None
    new_session_token: str | None = None
    clear_session_token: bool = False
    last_room: int | None = None
    params: Params = field(default_factory=Params)
    body: bytes = b""
    _query: list[tuple[str, str]] | None = None

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}"

    @property
    def full_path(self) -> str:
        return self.path + ("?" + self.query_string if self.query_string else "")

    @property
    def secure(self) -> bool:
        return self.scheme == "https"

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name, default)

    def query_pairs(self) -> list[tuple[str, str]]:
        if self._query is None:
            self._query = parse_qsl(self.query_string, keep_blank_values=True)
        return self._query

    def query(self, name: str, default: str = "") -> str:
        for key, found in self.query_pairs():
            if key == name:
                return found
        return default

    def wants_json(self) -> bool:
        return "application/json" in self.header("accept")

    @property
    def user(self) -> User:
        """The authenticated user; handlers call this only behind `login_required`."""
        assert self.current_user is not None
        return self.current_user


CONTEXT_KEY = "campfire.context"


def context_of(request: Request) -> RequestContext:
    found: object = request.scope[CONTEXT_KEY]
    assert isinstance(found, RequestContext)
    return found
