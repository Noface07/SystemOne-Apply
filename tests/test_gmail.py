"""Verification e-mails: the code or the link is found, the portal's own sender is preferred."""

import base64

from jev_apply import gmail


def part(mime, text):
    return {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}


def test_codes_and_links_are_found_in_portal_mail():
    assert gmail.code_in("Your verification code", "Use 482913 to verify your account.") == "482913"
    assert gmail.code_in("Sign in", "Your one-time passcode is: 7731. It expires in 10 minutes.") == "7731"
    assert gmail.code_in("Welcome", "Thanks for applying on 12 Oct 2026.") is None  # a date isn't a code
    html = '<p>Please <a href="https://xylem.wd5.myworkdayjobs.com/verify?token=abc">verify your email</a></p>'
    text = gmail.body_text({"mimeType": "multipart/alternative", "parts": [part("text/html", html)]})
    assert gmail.link_in(text) == "https://xylem.wd5.myworkdayjobs.com/verify?token=abc"
    assert gmail.link_in("https://x.test/unsubscribe?account=1") is None


def test_workday_mail_counts_as_the_portals_own():
    senders, tenant = gmail.senders_for("https://xylem.wd5.myworkdayjobs.com/en-US/xylem-careers/login")
    assert "myworkday.com" in senders and tenant == "xylem"
    assert "oracle.com" in gmail.senders_for("https://fa-x.fa.ocs.oraclecloud.com/hcmUI/x")[0]


def test_ordinary_words_and_foreign_links_are_never_taken():
    assert gmail.code_in("Samsung's best chip quarter", "Read the latest shorts here") is None
    assert gmail.code_in("Your code", "Your security code is AB12CD.") == "AB12CD"
    url = "https://xylem.wd5.myworkdayjobs.com/en-US/xylem-careers/login"
    assert gmail.link_in("verify: https://evil.test/verify?token=1", url) is None  # not the portal's site
    assert gmail.link_in("https://xylem.wd5.myworkdayjobs.com/verify?token=1", url).startswith("https://xylem")
    # Another company's Workday activation link is never opened for this one (Motorola's for Siemens)
    moto = "https://motorolasolutions.wd5.myworkdayjobs.com/careers/activate/hb7y7"
    assert gmail.link_in(moto, "https://onehealthineers.wd3.myworkdayjobs.com/en-US/shsjb/login") is None
    assert gmail.link_in(moto, "https://motorolasolutions.wd5.myworkdayjobs.com/en-US/careers/login") == moto


def test_the_sign_up_forms_verify_password_box_is_not_an_email_verification():
    from jev_apply import accounts

    assert not accounts.VERIFY.search("Email Address Password Verify New Password Create Account")
    assert accounts.VERIFY.search("We have sent you a verification email. Please verify your email to continue.")
