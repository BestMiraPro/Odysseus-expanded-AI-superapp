from services.youtube.youtube_handler import is_youtube_url


def test_is_youtube_url_handles_non_string():
    # `"youtube.com" in url` raises TypeError on a non-string url.
    assert is_youtube_url(123) is False
    assert is_youtube_url(None) is False
    assert is_youtube_url(["https://youtu.be/x"]) is False


def test_is_youtube_url_detects_real_urls():
    assert is_youtube_url("https://www.youtube.com/watch?v=x") is True
    assert is_youtube_url("https://youtu.be/x") is True


def test_youtube_host_is_not_a_substring():
    # Security plan S3: classification must be a parsed-host check.
    assert not is_youtube_url("https://evil.example/?next=youtube.com")
    assert not is_youtube_url("https://youtube.com.evil.example/watch?v=x")
    assert not is_youtube_url("https://notyoutube.com/watch?v=x")
    assert is_youtube_url("https://music.youtube.com/watch?v=x")
    assert is_youtube_url("https://youtu.be/x")


def test_youtube_url_rejects_userinfo_and_wrong_scheme():
    assert not is_youtube_url("https://user@youtube.com/watch?v=x")
    assert not is_youtube_url("https://youtube.com@evil.example/watch?v=x")
    assert not is_youtube_url("ftp://www.youtube.com/watch?v=x")
    assert not is_youtube_url("youtube.com/watch?v=x")  # no scheme


def test_malformed_urls_are_false_not_raising():
    assert is_youtube_url("https://[::1") is False
    assert is_youtube_url("https://youtube.com:notaport/x") is False
