from onvif.client import strip_user_pass_url, obscure_user_pass_url


def test_strip_user_pass_url():
    assert strip_user_pass_url("http://1.2.3.4/?user=foo&pass=bar") == "http://1.2.3.4/"
    assert strip_user_pass_url("http://1.2.3.4/") == "http://1.2.3.4/"


def test_obscure_user_pass_url():
    assert (
        obscure_user_pass_url("http://1.2.3.4/?user=foo&pass=bar")
        == "http://1.2.3.4/?user=********&pass=********"
    )
    assert obscure_user_pass_url("http://1.2.3.4/") == "http://1.2.3.4/"
