#!/usr/bin/env python3
# ruff: noqa: T201, PLR0915, PLC0415, SIM108
"""
VK Music Token Retrieval Script.

This script helps obtain an access token for VK Music service
using the vkpymusic library. The token is required for the
VK Music provider in Music Assistant.

Usage:
    python scripts/get_vk_token.py [--oauth]

Options:
    --oauth    Use browser-based OAuth flow (recommended if blocked)

Methods:
    1. Direct login (default) - Uses vkpymusic TokenReceiver
    2. OAuth browser flow - Opens browser, you log in via VK interface

The OAuth method bypasses brute-force protection because authentication
happens through VK's own interface, not through API calls.

Requirements:
    pip install vkpymusic
"""

from __future__ import annotations

import getpass
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

# Anti-bruteforce: Add delays between operations
DELAY_BEFORE_AUTH = 2  # seconds

# Kate Mobile client credentials (required for audio API access)
KATE_CLIENT_ID = "2685278"
KATE_CLIENT_SECRET = "lxhD8OD7dMsqtXIm5IUY"
KATE_USER_AGENT = (
    "KateMobileAndroid/56 lite-460 "
    "(Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)"
)

# OAuth configuration - use VK's blank.html as redirect (allowed for all apps)
OAUTH_REDIRECT_URI = "https://oauth.vk.com/blank.html"


def auth_oauth() -> str | None:
    """Perform OAuth browser-based authentication with manual token paste."""
    print()
    print("=" * 60)
    print("OAuth Browser Authentication")
    print("=" * 60)
    print()
    print("This method opens your browser for VK login.")
    print("Benefits:")
    print("  - Bypasses brute-force protection")
    print("  - Handles 2FA automatically")
    print("  - More secure (password not entered in terminal)")
    print()

    # Build OAuth URL using VK's blank.html redirect (works for any app)
    oauth_params = {
        "client_id": KATE_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "display": "page",
        "scope": "audio,offline",
        "response_type": "token",
        "v": "5.131",
    }

    oauth_url = "https://oauth.vk.com/authorize?" + urllib.parse.urlencode(oauth_params)

    print("Opening browser for VK authorization...")
    print()
    print("=" * 60)
    print("INSTRUCTIONS:")
    print("=" * 60)
    print()
    print("1. Log in to VK in the browser that opens")
    print("2. Allow access to the application")
    print("3. You will be redirected to a blank page")
    print("4. Copy the ENTIRE URL from the browser address bar")
    print("5. Paste it here")
    print()
    print("The URL will look like:")
    print("https://oauth.vk.com/blank.html#access_token=vk1.a.xxx...")
    print()
    print("-" * 60)
    print()

    # Open browser
    webbrowser.open(oauth_url)

    print("If browser didn't open, manually visit:")
    print(oauth_url)
    print()
    print("-" * 60)
    print()

    # Get URL from user
    redirect_url = input("Paste the redirect URL here: ").strip()

    if not redirect_url:
        print("Error: No URL provided")
        return None

    # Parse token from URL fragment
    # URL format: https://oauth.vk.com/blank.html#access_token=xxx&expires_in=0&user_id=123
    token = None

    # Try to extract from fragment (#)
    if "#" in redirect_url:
        fragment = redirect_url.split("#", 1)[1]
        params = urllib.parse.parse_qs(fragment)

        if "access_token" in params:
            token = params["access_token"][0]
        elif "error" in params:
            error = params.get("error_description", params.get("error", ["Unknown"]))
            print()
            print(f"VK returned error: {error[0]}")
            return None

    # Also try query string (?) just in case
    if not token and "?" in redirect_url:
        query = urllib.parse.urlparse(redirect_url).query
        params = urllib.parse.parse_qs(query)
        if "access_token" in params:
            token = params["access_token"][0]

    # Try to extract token directly if user pasted just the token
    if not token and redirect_url.startswith("vk1.a."):
        token = redirect_url

    if token:
        print()
        print("=" * 60)
        print("SUCCESS!")
        print("=" * 60)
        return token
    print()
    print("Could not extract token from URL.")
    print("Make sure you copied the entire URL after authorization.")
    return None


def auth_direct() -> str | None:
    """Perform direct login authentication using vkpymusic."""
    try:
        from vkpymusic import TokenReceiver
    except ImportError:
        print("Error: vkpymusic is not installed.")
        print("Install it with: pip install vkpymusic")
        return None

    print()
    print("=" * 60)
    print("Direct Login Authentication")
    print("=" * 60)
    print()
    print("IMPORTANT: VK has brute-force protection!")
    print()
    print("Before continuing:")
    print("  1. Make sure your password is 100% correct")
    print("  2. If you have 2FA, have your phone/app ready")
    print("  3. If blocked, use --oauth method instead")
    print()
    print("-" * 60)
    print()

    # Get credentials
    login = input("Enter VK login (phone or email): ").strip()
    if not login:
        print("Error: Login cannot be empty")
        return None

    confirm = input(f"Confirm login '{login}' is correct (y/n): ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return None

    print()
    try:
        password = getpass.getpass("Enter VK password (hidden): ")
    except Exception:
        password = input("Enter VK password: ").strip()

    if not password:
        print("Error: Password cannot be empty")
        return None

    try:
        password_confirm = getpass.getpass("Confirm password (hidden): ")
    except Exception:
        password_confirm = input("Confirm password: ").strip()

    if password != password_confirm:
        print("Error: Passwords don't match!")
        return None

    print()
    print(f"Starting authentication in {DELAY_BEFORE_AUTH} seconds...")
    time.sleep(DELAY_BEFORE_AUTH)

    receiver = TokenReceiver(login, password, client="Kate")
    bruteforce_detected = False

    def on_captcha(captcha_url: str) -> str:
        print()
        print("CAPTCHA REQUIRED")
        print(f"Open: {captcha_url}")
        result = input("Enter captcha text: ").strip()
        time.sleep(1)
        return result

    def on_2fa() -> str:
        print()
        print("2FA CODE REQUIRED")
        result = input("Enter code from SMS/app: ").strip()
        time.sleep(1)
        return result

    def on_invalid_client() -> None:
        print()
        print("Invalid login or password!")

    def on_critical_error(response: object) -> None:
        nonlocal bruteforce_detected
        response_str = str(response).lower()
        if "bruteforce" in response_str or "flood" in response_str:
            bruteforce_detected = True
            print()
            print("BRUTE-FORCE PROTECTION TRIGGERED!")
            print("Try using: python scripts/get_vk_token.py --oauth")
        else:
            print(f"Error: {response}")

    print("Authenticating...")
    success = receiver.auth(
        on_captcha=on_captcha,
        on_2fa=on_2fa,
        on_invalid_client=on_invalid_client,
        on_critical_error=on_critical_error,
    )

    if not success:
        if bruteforce_detected:
            print()
            print("Recommendation: Use OAuth method instead")
            print("Run: python scripts/get_vk_token.py --oauth")
        return None

    print()
    print("=" * 60)
    print("SUCCESS!")
    print("=" * 60)

    return receiver.get_token()


def save_token(token: str) -> None:
    """Display and optionally save the token."""
    print()
    print("What would you like to do with the token?")
    print()
    print("  1. Display token (copy manually)")
    print("  2. Save to config file (config_vk.ini)")
    print("  3. Both")
    print()

    choice = input("Enter choice (1/2/3) [default: 3]: ").strip() or "3"

    if choice in ("1", "3"):
        print()
        print("Your VK Music token:")
        print("-" * 60)
        print(token)
        print("-" * 60)
        print()
        print("Use this token in Music Assistant VK Music provider.")

    if choice in ("2", "3"):
        config_path = Path(__file__).parent / "config_vk.ini"
        with open(config_path, "w") as f:
            f.write("[VK]\n")
            f.write(f"user_agent={KATE_USER_AGENT}\n")
            f.write(f"token_for_audio={token}")
        print()
        print(f"Token saved to: {config_path}")

    print()
    print("=" * 60)
    print("Done!")
    print("=" * 60)


def main() -> None:
    """Run the VK token retrieval process."""
    print()
    print("=" * 60)
    print("VK Music Token Retrieval")
    print("=" * 60)

    # Check for --oauth flag
    use_oauth = "--oauth" in sys.argv

    if not use_oauth:
        print()
        print("Choose authentication method:")
        print()
        print("  1. Direct login (enter credentials here)")
        print("  2. OAuth browser (login via VK website) [RECOMMENDED]")
        print()
        print("OAuth is recommended because:")
        print("  - Bypasses brute-force protection")
        print("  - Handles 2FA automatically")
        print("  - More secure")
        print()

        method = input("Enter choice (1/2) [default: 2]: ").strip() or "2"
        use_oauth = method == "2"

    if use_oauth:
        token = auth_oauth()
    else:
        token = auth_direct()

    if token:
        save_token(token)
    else:
        print()
        print("Failed to obtain token.")
        sys.exit(1)


if __name__ == "__main__":
    main()
