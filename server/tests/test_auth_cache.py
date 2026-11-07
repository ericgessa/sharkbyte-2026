import base64
import json
from types import SimpleNamespace

from app.services.auth import SupabaseAuth


def jwt(exp: float | None, user="u") -> str:
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    claims = {"sub": user} | ({"exp": exp} if exp is not None else {})
    return f"{b64({'alg': 'ES256'})}.{b64(claims)}.sig"


class FakeSupabaseClient:
    """Counts how many times Supabase Auth is asked. Tokens containing 'bad' are rejected."""

    def __init__(self):
        self.calls = 0
        self.auth = SimpleNamespace(get_user=self._get_user)

    def _get_user(self, token):
        self.calls += 1
        if "bad" in token:
            raise RuntimeError("invalid JWT")
        return SimpleNamespace(user=SimpleNamespace(id=f"user-of-{token[-6:]}"))


def make(ttl=60.0, max_entries=1000, t=None):
    t = t or [1000.0]
    client = FakeSupabaseClient()
    auth = SupabaseAuth(client, ttl_s=ttl, max_entries=max_entries, now=lambda: t[0], wall_clock=lambda: 1_800_000_000.0)
    return auth, client, t


def test_a_repeat_request_with_the_same_token_does_not_call_supabase_again():
    auth, client, _ = make()
    tok = jwt(1_800_003_600)
    assert auth.verify(tok) == auth.verify(tok) == auth.verify(tok)
    assert client.calls == 1


def test_the_cache_expires_and_the_token_is_re_checked():
    auth, client, t = make(ttl=60)
    tok = jwt(1_800_003_600)
    auth.verify(tok)
    t[0] += 59
    auth.verify(tok)
    assert client.calls == 1
    t[0] += 2  # past 60 s
    auth.verify(tok)
    assert client.calls == 2


def test_failures_are_never_cached():
    auth, client, _ = make()
    assert auth.verify("bad-token") is None and auth.verify("bad-token") is None
    assert client.calls == 2  # a bad token is re-checked every time, so a token that becomes valid works at once


def test_the_cache_never_outlives_the_tokens_own_expiry():
    auth, client, t = make(ttl=3600)
    soon = jwt(1_800_000_030)  # the token expires 30 s from the (fake) wall clock
    auth.verify(soon)
    t[0] += 26  # inside the 5 s safety margin before the real expiry: must be re-checked
    auth.verify(soon)
    assert client.calls == 2


def test_an_already_expired_or_nearly_expired_token_is_not_cached_at_all():
    auth, client, _ = make()
    auth.verify(jwt(1_799_999_000))  # expired in the past
    auth.verify(jwt(1_799_999_000))
    assert client.calls == 2


def test_tokens_without_an_exp_claim_still_get_the_default_ttl():
    auth, client, _ = make()
    tok = "opaque.token.value"
    auth.verify(tok), auth.verify(tok)
    assert client.calls == 1


def test_different_tokens_and_users_are_kept_apart():
    auth, client, _ = make()
    a, b = jwt(1_800_003_600, "a") + "AAAAAA", jwt(1_800_003_600, "b") + "BBBBBB"
    assert auth.verify(a) != auth.verify(b)
    assert auth.verify(a) == "user-of-AAAAAA" and auth.verify(b) == "user-of-BBBBBB"
    assert client.calls == 2


def test_the_raw_token_is_never_stored_in_the_cache():
    auth, _, _ = make()
    tok = jwt(1_800_003_600, "secret-subject") + "SECRETTAIL"
    auth.verify(tok)
    dump = repr(list(auth._cache.items()))
    assert tok not in dump and "SECRETTAIL" not in dump


def test_invalidating_a_user_drops_all_of_their_cached_tokens_immediately():
    auth, client, _ = make()
    t1, t2 = jwt(1_800_003_600) + "111111", jwt(1_800_003_600, "x") + "111111"  # same user id suffix
    other = jwt(1_800_003_600) + "222222"
    uid = auth.verify(t1)
    auth.verify(t2), auth.verify(other)
    calls = client.calls
    auth.invalidate_user(uid)
    auth.verify(t1), auth.verify(t2)  # both belonged to that user: re-checked
    auth.verify(other)  # someone else's: still cached
    assert client.calls == calls + 2


def test_the_cache_is_bounded():
    auth, _, _ = make(max_entries=3)
    for i in range(10):
        auth.verify(jwt(1_800_003_600) + f"{i:06d}")
    assert len(auth._cache) == 3
