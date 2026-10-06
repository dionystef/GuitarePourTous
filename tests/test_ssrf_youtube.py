"""Tests de la validation d'URL YouTube et des gardes SSRF (services/downloader)."""
import services.downloader as dl


def _set_getaddrinfo(monkeypatch, *rows):
    """Fait résoudre tout hôte vers les IP données (chaque row = (host, port))."""

    def fake(host, port):
        return [(0, 0, 0, "", (ip, port)) for ip in rows]

    monkeypatch.setattr(dl.socket, "getaddrinfo", fake)


# --------------------------------------------------------------------------- #
# validate_youtube_url
# --------------------------------------------------------------------------- #
def test_validate_url_accepts_youtube_variants():
    assert dl.validate_youtube_url("https://www.youtube.com/watch?v=abc") is True
    assert dl.validate_youtube_url("https://youtube.com/watch?v=abc") is True
    assert dl.validate_youtube_url("http://youtu.be/abc") is True
    assert dl.validate_youtube_url("https://m.youtube.com/watch?v=x") is True
    assert dl.validate_youtube_url("https://sub.youtube.com/x") is True
    assert dl.validate_youtube_url("https://youtube.com:8080/x") is True


def test_validate_url_rejects_non_youtube_hosts():
    assert dl.validate_youtube_url("https://notyoutube.com/x") is False
    assert dl.validate_youtube_url("https://youtube.com.evil.com/x") is False
    assert dl.validate_youtube_url("https://evil.com/youtube.com") is False


def test_validate_url_rejects_private_and_ip_hosts():
    assert dl.validate_youtube_url("http://192.168.1.1/watch") is False
    assert dl.validate_youtube_url("http://10.0.0.1/x") is False
    assert dl.validate_youtube_url("http://169.254.1.1/x") is False
    assert dl.validate_youtube_url("http://localhost/watch") is False
    assert dl.validate_youtube_url("http://[::1]/x") is False


def test_validate_url_rejects_embedded_credentials_and_schemes():
    assert dl.validate_youtube_url("https://youtube.com@evil.com/x") is False
    assert dl.validate_youtube_url("ftp://youtube.com/x") is False
    assert dl.validate_youtube_url("") is False


# --------------------------------------------------------------------------- #
# _is_private_or_local_ip
# --------------------------------------------------------------------------- #
def test_is_private_or_local_ip():
    assert dl._is_private_or_local_ip("127.0.0.1") is True
    assert dl._is_private_or_local_ip("10.0.0.5") is True
    assert dl._is_private_or_local_ip("192.168.1.1") is True
    assert dl._is_private_or_local_ip("169.254.10.1") is True      # link-local
    assert dl._is_private_or_local_ip("224.0.0.1") is True        # multicast
    assert dl._is_private_or_local_ip("0.0.0.0") is True          # unspecified
    assert dl._is_private_or_local_ip("::1") is True              # loopback IPv6
    assert dl._is_private_or_local_ip("8.8.8.8") is False         # public
    assert dl._is_private_or_local_ip("not-an-ip") is False


# --------------------------------------------------------------------------- #
# _reject_non_public
# --------------------------------------------------------------------------- #
def test_reject_non_public_raises_on_literal_private():
    import pytest

    with pytest.raises(RuntimeError):
        dl._reject_non_public("http://127.0.0.1/x")


def test_reject_non_public_dns_resolves_private(monkeypatch):
    import pytest

    _set_getaddrinfo(monkeypatch, "10.0.0.7")
    with pytest.raises(RuntimeError):
        dl._reject_non_public("http://youtube.com/x")


def test_reject_non_public_dns_resolves_public_ok(monkeypatch):
    _set_getaddrinfo(monkeypatch, "142.250.72.14")  # IP publique arbitraire
    # Ne doit pas lever.
    dl._reject_non_public("http://youtube.com/x")


# --------------------------------------------------------------------------- #
# match_filter
# --------------------------------------------------------------------------- #
def test_match_filter_accepts_youtube():
    assert dl._youtube_match_filter({"extractor_key": "Youtube"}) is None
    assert dl._youtube_match_filter({"extractor": "youtube"}) is None


def test_match_filter_rejects_generic():
    reason = dl._youtube_match_filter({"extractor_key": "Generic"})
    assert reason is not None and "non YouTube" in reason


# --------------------------------------------------------------------------- #
# _youtube_ydl_opts
# --------------------------------------------------------------------------- #
def test_youtube_ydl_opts_includes_guards():
    opts = dl._youtube_ydl_opts()
    assert opts["socket_timeout"] == dl.SOCKET_TIMEOUT
    assert opts["noplaylist"] is True
    assert opts["match_filter"] is dl._youtube_match_filter
    # Les options additionnelles sont fusionnées.
    opts2 = dl._youtube_ydl_opts(skip_download=True)
    assert opts2["skip_download"] is True
