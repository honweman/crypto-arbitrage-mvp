from __future__ import annotations

import html

from .users import WebUser


AUTH_CSS = """
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    display: grid;
    place-items: center;
    padding: 20px;
    background: #f4f6f8;
    color: #17211b;
    font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  main { width: min(440px, 100%); display: grid; gap: 14px; }
  form, .panel {
    display: grid;
    gap: 12px;
    padding: 22px;
    border: 1px solid #d8ded8;
    border-radius: 8px;
    background: #ffffff;
    box-shadow: 0 12px 32px rgba(17, 24, 39, 0.06);
  }
  h1 { margin: 0; font-size: 21px; letter-spacing: 0; }
  p { margin: 0; color: #66736b; font-size: 13px; line-height: 1.5; }
  label { color: #4f5c54; font-size: 12px; font-weight: 700; }
  input {
    width: 100%;
    min-height: 42px;
    padding: 8px 10px;
    border: 1px solid #cfd7d1;
    border-radius: 6px;
    background: #ffffff;
    color: #17211b;
    font: inherit;
  }
  input:focus { outline: 2px solid #a8c7b4; outline-offset: 1px; }
  button {
    min-height: 42px;
    padding: 8px 12px;
    border: 1px solid #17211b;
    border-radius: 6px;
    background: #17211b;
    color: #ffffff;
    font: inherit;
    font-weight: 700;
    cursor: pointer;
  }
  button.secondary { background: #ffffff; color: #17211b; }
  a { color: #1d4ed8; font-size: 13px; text-decoration: none; }
  .links { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 10px; }
  .error, .notice { min-height: 18px; font-size: 13px; line-height: 1.4; }
  .error { color: #b42318; }
  .notice { color: #17633a; }
  .rule { color: #66736b; font-size: 12px; }
  .setup { display: grid; gap: 8px; padding: 12px 0; border-block: 1px solid #e5e9e6; }
  code { overflow-wrap: anywhere; color: #17211b; font-size: 14px; }
"""


def _auth_document(*, title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
  <style>{AUTH_CSS}</style>
</head>
<body><main>{body}</main></body>
</html>
"""


def login_html(
    *,
    error: str = "",
    email_login: bool = False,
    registration_enabled: bool = False,
) -> str:
    error_html = f'<div class="error">{html.escape(error)}</div>'
    register_link = (
        '<a href="/register">邮箱注册 / Register</a>'
        if registration_enabled
        else ""
    )
    if email_login:
        body = f"""
<form method="post" action="/login">
  <h1>Crypto Trading</h1>
  <p>使用登录名和密码进入交易后台。</p>
  <label for="username">登录名 / Username</label>
  <input id="username" name="username" type="text" autocomplete="username" autofocus required>
  <label for="password">密码 / Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <label for="totp">Authenticator 动态码 / Authenticator code</label>
  <input id="totp" name="totp" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6}}" maxlength="6">
  <div class="rule">仅在账户已启用二次验证时需要填写。</div>
  <button type="submit">登录 / Sign In</button>
  <div class="links">
    {register_link}
    <a href="/forgot-password">忘记密码 / Forgot password</a>
  </div>
  {error_html}
</form>
"""
    else:
        body = f"""
<form method="post" action="/login">
  <h1>Crypto Trading</h1>
  <p>尚未创建邮箱账户。可先注册，或使用临时后台密码。</p>
  <label for="password">临时密码 / Temporary password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" autofocus required>
  <button type="submit">登录 / Sign In</button>
  <div class="links">{register_link}</div>
  {error_html}
</form>
"""
    return _auth_document(title="Crypto Trading Login", body=body)


def register_html(
    *,
    error: str = "",
    notice: str = "",
    email: str = "",
    username: str = "",
    user: WebUser | None = None,
) -> str:
    if user is not None:
        body = f"""
<div class="panel">
  <h1>注册成功 / Registered</h1>
  <p>登录名：<strong>{html.escape(user.username)}</strong></p>
  <p>邮箱：{html.escape(user.email)}</p>
  <a href="/login">返回登录 / Continue to login</a>
</div>
"""
    else:
        body = f"""
<form method="post" action="/register">
  <h1>邮箱注册 / Register</h1>
  <p>验证码会发送到邮箱。注册后使用登录名和密码登录。</p>
  <label for="email">邮箱 / Email</label>
  <input id="email" name="email" type="email" value="{html.escape(email)}" autocomplete="email" autofocus required>
  <label for="username">登录名 / Username</label>
  <input id="username" name="username" type="text" value="{html.escape(username)}" autocomplete="username" minlength="3" maxlength="32" pattern="[A-Za-z0-9][A-Za-z0-9_.-]{{2,31}}" required>
  <label for="verification_code">邮箱验证码 / Verification code</label>
  <input id="verification_code" name="verification_code" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6}}" maxlength="6" required>
  <button class="secondary" type="submit" formaction="/register/code" formnovalidate>发送验证码 / Send code</button>
  <label for="password">密码 / Password</label>
  <input id="password" name="password" type="password" autocomplete="new-password" minlength="8" required>
  <label for="password_confirm">确认密码 / Confirm password</label>
  <input id="password_confirm" name="password_confirm" type="password" autocomplete="new-password" minlength="8" required>
  <div class="rule">至少 8 位，并同时包含字母、数字和特殊符号。</div>
  <button type="submit">完成注册 / Create account</button>
  <div class="links"><a href="/login">返回登录 / Back to login</a></div>
  <div class="notice">{html.escape(notice)}</div>
  <div class="error">{html.escape(error)}</div>
</form>
"""
    return _auth_document(title="Register Crypto Trading User", body=body)


def forgot_password_html(
    *,
    error: str = "",
    notice: str = "",
    email: str = "",
    reset_complete: bool = False,
) -> str:
    if reset_complete:
        body = """
<div class="panel">
  <h1>密码已更新 / Password updated</h1>
  <p>请使用登录名和新密码重新登录。原 Authenticator 绑定已重置，可登录后重新绑定。</p>
  <a href="/login">返回登录 / Continue to login</a>
</div>
"""
    else:
        body = f"""
<form method="post" action="/reset-password">
  <h1>找回密码 / Reset password</h1>
  <p>输入注册邮箱获取验证码。为保护账户，页面不会显示该邮箱是否已注册。</p>
  <label for="email">注册邮箱 / Email</label>
  <input id="email" name="email" type="email" value="{html.escape(email)}" autocomplete="email" autofocus required>
  <label for="verification_code">邮箱验证码 / Verification code</label>
  <input id="verification_code" name="verification_code" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6}}" maxlength="6" required>
  <button class="secondary" type="submit" formaction="/forgot-password/code" formnovalidate>发送验证码 / Send code</button>
  <label for="password">新密码 / New password</label>
  <input id="password" name="password" type="password" autocomplete="new-password" minlength="8" required>
  <label for="password_confirm">确认新密码 / Confirm password</label>
  <input id="password_confirm" name="password_confirm" type="password" autocomplete="new-password" minlength="8" required>
  <div class="rule">至少 8 位，并同时包含字母、数字和特殊符号。</div>
  <button type="submit">更新密码 / Update password</button>
  <div class="links"><a href="/login">返回登录 / Back to login</a></div>
  <div class="notice">{html.escape(notice)}</div>
  <div class="error">{html.escape(error)}</div>
</form>
"""
    return _auth_document(title="Reset Crypto Trading Password", body=body)


def _account_management_html(user: WebUser, pending_new_email: str = "") -> str:
    totp_field = (
        '<label for="acct-totp">Authenticator 动态码 / Authenticator code</label>\n'
        '  <input id="acct-totp" name="totp" type="text" inputmode="numeric" '
        'autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required>'
        if user.totp_enabled
        else ""
    )
    delete_totp_field = totp_field.replace("acct-totp", "del-totp")
    if pending_new_email:
        change_email_form = f"""
<form method="post" action="/security">
  <h1>确认新邮箱 / Confirm new email</h1>
  <p>验证码已发送至 <code>{html.escape(pending_new_email)}</code>。</p>
  <input type="hidden" name="action" value="confirm_email_change">
  <input type="hidden" name="new_email" value="{html.escape(pending_new_email, quote=True)}">
  <label for="change-code">6 位验证码 / 6-digit code</label>
  <input id="change-code" name="code" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6}}" maxlength="6" required>
  <button type="submit">确认更换邮箱 / Confirm email change</button>
  <div class="rule">更换后所有会话将注销，交易所 API 凭证需重新录入。</div>
</form>
"""
    else:
        change_email_form = f"""
<form method="post" action="/security">
  <h1>更换登录邮箱 / Change login email</h1>
  <p>当前邮箱 / Current email: <code>{html.escape(user.email)}</code></p>
  <input type="hidden" name="action" value="request_email_change">
  <label for="new-email">新邮箱 / New email</label>
  <input id="new-email" name="new_email" type="email" autocomplete="email" required>
  <label for="change-password">当前密码 / Current password</label>
  <input id="change-password" name="password" type="password" autocomplete="current-password" required>
  {totp_field}
  <button type="submit">发送验证码 / Send verification code</button>
  <div class="rule">验证码会发送到新邮箱。更换后需重新登录，且出于加密安全，交易所 API 凭证需重新录入。</div>
</form>
"""
    delete_form = f"""
<form method="post" action="/security">
  <h1>删除账户 / Delete account</h1>
  <p>删除后账户与全部数据（项目、交易所账户、策略、模拟盘与回测记录）将立即清除，且无法恢复。</p>
  <input type="hidden" name="action" value="delete_account">
  <label for="del-password">当前密码 / Current password</label>
  <input id="del-password" name="password" type="password" autocomplete="current-password" required>
  {delete_totp_field}
  <label class="rule"><input type="checkbox" name="confirm_delete" required> 我确认永久删除该账户及全部数据 / I confirm permanent deletion</label>
  <button type="submit">永久删除账户 / Permanently delete account</button>
</form>
"""
    return change_email_form + delete_form


def security_html(
    *,
    user: WebUser,
    issuer: str,
    provisioning_uri: str = "",
    error: str = "",
    notice: str = "",
    signed_out: bool = False,
    pending_new_email: str = "",
) -> str:
    error_html = f'<div class="error">{html.escape(error)}</div>'
    notice_html = f'<div class="notice">{html.escape(notice)}</div>'
    if signed_out:
        body = f"""
<div class="panel">
  <h1>安全设置已更新 / Security updated</h1>
  {notice_html}
  <p>所有旧会话已经失效，请重新登录。</p>
  <a href="/login">重新登录 / Sign in again</a>
</div>
"""
    elif user.totp_enabled:
        body = f"""
<form method="post" action="/security">
  <h1>登录安全 / Login security</h1>
  <p>Google Authenticator 二次验证已启用。</p>
  <input type="hidden" name="action" value="disable">
  <input type="text" name="username" value="{html.escape(user.username)}" autocomplete="username" hidden readonly>
  <label for="password">当前密码 / Current password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <label for="totp">Authenticator 动态码 / Authenticator code</label>
  <input id="totp" name="totp" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6}}" maxlength="6" required>
  <button type="submit">关闭二次验证 / Disable 2FA</button>
  <div class="rule">关闭后会注销所有现有会话并生成新的绑定密钥。</div>
  <div class="links"><a href="/">返回控制台 / Back to dashboard</a></div>
  {notice_html}
  {error_html}
</form>
"""
    else:
        secret = html.escape(user.totp_secret)
        uri = html.escape(provisioning_uri, quote=True)
        body = f"""
<form method="post" action="/security">
  <h1>启用二次验证 / Enable 2FA</h1>
  <p>在 Google Authenticator 中添加基于时间的密钥，然后输入生成的 6 位动态码。</p>
  <div class="setup">
    <label>账户 / Account</label>
    <code>{html.escape(user.username)}</code>
    <label>发行方 / Issuer</label>
    <code>{html.escape(issuer)}</code>
    <label>设置密钥 / Setup key</label>
    <code>{secret}</code>
    <a href="{uri}">在 Authenticator 中打开 / Open in authenticator</a>
  </div>
  <input type="hidden" name="action" value="enable">
  <input type="text" name="username" value="{html.escape(user.username)}" autocomplete="username" hidden readonly>
  <label for="password">当前密码 / Current password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <label for="totp">6 位动态码 / 6-digit code</label>
  <input id="totp" name="totp" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6}}" maxlength="6" required>
  <button type="submit">验证并启用 / Verify and enable</button>
  <div class="rule">绑定成功会注销现有会话。遗失手机时，可通过邮箱找回密码并重置二次验证。</div>
  <div class="links"><a href="/">暂不启用 / Back to dashboard</a></div>
  {notice_html}
  {error_html}
</form>
"""
    if not signed_out:
        body += _account_management_html(user, pending_new_email)
    return _auth_document(title="Crypto Trading Login Security", body=body)
