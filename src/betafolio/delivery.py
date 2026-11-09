"""Report rendering and explicit, owner-only SMTP delivery."""

from __future__ import annotations
from email.message import EmailMessage
from datetime import date, datetime
from html import escape
import re
import smtplib
import ssl
from typing import Any, Literal
from zoneinfo import ZoneInfo
from .metrics import daily_change_pct
from pydantic import BaseModel, ConfigDict, Field, field_validator


class SMTPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    port: int = Field(default=465, ge=1, le=65535)
    username: str = ""
    sender: str
    recipient: str
    tls_mode: Literal["implicit", "starttls"] = "implicit"
    plain_text_only: bool = False
    percent_only: bool = False

    @field_validator("sender", "recipient")
    @classmethod
    def single_address(cls, value: str) -> str:
        # Bare addresses only: no lists, display names, or header injection.
        if not re.fullmatch(r"[^\s@<>,;:\"\\]+@[^\s@<>,;:\"\\]+\.[^\s@<>,;:\"\\]+", value):
            raise ValueError("Enter one bare email address")
        return value

    @field_validator("host", "username")
    @classmethod
    def no_newlines(cls, value: str) -> str:
        if any(c in value for c in "\r\n\x00"):
            raise ValueError("Invalid connection setting")
        return value


class DeliveryOutcome(BaseModel):
    status: Literal["sent", "failed", "uncertain"]
    message: str


def _data(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def human_date(value, timezone="America/New_York", full=False):
    raw = str(value or "")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed = date.fromisoformat(raw)
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ZoneInfo(timezone))
        return (
            f"{parsed:%B} {parsed.day}, {parsed.year}" if full else f"{parsed:%b} {parsed.day}, {parsed.year}"
        )
    except (ValueError, TypeError):
        return "Date unavailable"


def briefing_summary(result):
    data = _data(result)
    return data.get("executive_summary") or "Your portfolio briefing is available."


def _clean(text):
    # Keep the report readable; source links and access times live in the run trace.
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", str(text or ""))
    return re.sub(r"https?://\S+", "", text).strip()


def _prose(text):
    from markdown_it import MarkdownIt

    # Model content remains text. Render standard formatting consistently in
    # paragraphs, lists and tables; raw HTML is escaped, images are disabled.
    renderer = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")
    # Typographic bullets are valid editorial text but not Markdown list markers.
    # Normalize line-leading bullets only, preserving fenced code verbatim.
    lines = _clean(text).splitlines()
    fence = None
    for index, line in enumerate(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
            continue
        if fence is None:
            lines[index] = re.sub(r"^( {0,3})[\u2022\u25cf\u25aa][ \t\u00a0\u202f]+", r"\1- ", line)
    return (
        renderer.render("\n".join(lines))
        .replace("<table>", '<div class="table-scroll"><table>')
        .replace("</table>", "</table></div>")
    )


def _plain(text):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_prose(text), "html.parser")
    for br in soup.find_all('br'):
        br.replace_with('\n')
    for item in soup.find_all('li'):
        parent = item.parent
        if parent and parent.name == 'ol':
            siblings = parent.find_all('li', recursive=False)
            marker = f"{int(parent.get('start', 1)) + siblings.index(item)}. "
        else:
            marker = '- '
        # Loose Markdown lists wrap item text in a paragraph.
        (item.find('p', recursive=False) or item).insert(0, marker)
    for cell in soup.find_all(['td', 'th']):
        cell.append('\t')
    for block in soup.find_all(['p', 'div', 'ul', 'ol', 'blockquote', 'pre', 'tr',
                                'h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        block.append('\n')
    rendered = '\n'.join(line.strip() for line in soup.get_text().splitlines())
    return re.sub(r'\n{3,}', '\n\n', rendered).strip()


def signed_number(value, *, prefix="", suffix=""):
    from decimal import Decimal

    rounded = Decimal(str(value)).quantize(Decimal("0.01"))
    sign = "+" if rounded > 0 else "-" if rounded < 0 else ""
    return f"{sign}{prefix}{abs(rounded):,.2f}{suffix}"


def render_report(
    result: Any, percent_only: bool = False, embed: bool = False, timezone: str = "America/New_York"
):
    data = _data(result)
    metrics = data.get("metrics") or {}
    title = "Betafolio briefing — " + human_date(
        data.get("research_as_of") or data.get("created_at") or data.get("snapshot_as_of"), timezone, full=True
    )
    lines = [title]
    body = "<h1>" + escape(title) + "</h1>"
    if any(str(warning).startswith("DEMONSTRATION PORTFOLIO") for warning in data.get("warnings", [])):
        body += '<p class="note">Demonstration portfolio — illustrative holdings and prices.</p>'
        lines.append("Demonstration portfolio — illustrative holdings and prices.")
    if percent_only:
        body += "<h2>Portfolio allocation</h2><table>"
        weights = {**metrics.get("weights_pct", {}), "Cash": metrics.get("cash_weight_pct", 0)}
        for symbol, value in weights.items():
            line = f"{symbol}: {float(value):.1f}%"
            lines.append(line)
            body += f"<tr><th>{escape(str(symbol))}</th><td>{float(value):.1f}%</td></tr>"
        body += "</table>"
    else:
        summary = briefing_summary(data)
        body += "<h2>Executive summary</h2>" + _prose(summary)
        lines.extend(["Executive summary", _plain(summary)])
        for item in data.get("sections") or []:
            section = _data(item)
            heading, content = _clean(section["heading"]), _clean(section["content"])
            body += "<section><h2>" + escape(heading) + "</h2>" + _prose(content) + "</section>"
            lines.extend([heading, _plain(content)])
        if data.get("suggestions"):
            body += "<h2>Optional adjustments</h2>" + _prose(
                "\n".join("- " + value for value in data["suggestions"])
            )
            lines.extend(_plain(value) for value in data["suggestions"])
        positions = metrics.get("position_details") or []
        if positions:
            label = "Positions — " + human_date(metrics.get("price_session"), timezone)
            body += (
                "<h2>"
                + escape(label)
                + '</h2><div class="table-scroll"><table><thead><tr><th>Holding</th><th>Price</th><th>Day change</th><th>Value change</th></tr></thead><tbody>'
            )
            lines.append(label)
            changes = metrics.get("daily_price_contributions") or {}
            for row in positions:
                price = f"${float(row['price']):,.2f}" if row.get("price") is not None else "Unavailable"
                change = row.get("daily_change_pct") if "daily_change_pct" in row else daily_change_pct(
                    row.get("price"), row.get("previous_close"), row.get("current_session")
                )
                daily = signed_number(change, suffix="%") if change is not None else "Unavailable"
                amount = (
                    signed_number(changes[row['symbol']], prefix="$")
                    if row["symbol"] in changes and change is not None
                    else "—"
                )
                body += (
                    "<tr><th>"
                    + escape(row["symbol"])
                    + "</th><td>"
                    + escape(price)
                    + "</td><td>"
                    + escape(daily)
                    + "</td><td>"
                    + escape(amount)
                    + "</td></tr>"
                )
                lines.append(f"{row['symbol']}: {price}; day change {daily}; value change {amount}")
            body += "</tbody></table></div>"
        if metrics.get("price_note"):
            note = metrics["price_note"]
            body += '<p class="note">' + escape(note) + "</p>"
            lines.append(note)
        if data.get("error"):
            body += '<p class="note">' + escape(data["error"]) + "</p>"
            lines.append(data["error"])
    style = (
        ":root{color-scheme:light}*{box-sizing:border-box}"
        'body{font:16px/1.7 "DM Sans",Arial,sans-serif;color:#14213D;background:#F5F7FC;margin:0;padding:20px}'
        "main{max-width:800px;margin:auto;background:#FFFFFF;padding:clamp(20px,5vw,52px);border-top:4px solid #2563EB}"
        "h1{font:400 clamp(28px,4vw,38px)/1.15 \"DM Sans\",Arial,sans-serif;letter-spacing:-.03em;margin:0 0 28px}"
        "h2{font:400 23px/1.3 \"DM Sans\",Arial,sans-serif;margin:1.7em 0 .65em;color:#14213D}"
        "h3,h4,h5,h6{font-size:17px;line-height:1.4;margin:1.3em 0 .5em}"
        "p{margin:.6em 0 1em}strong{font-weight:650;color:#14213D}em{font-style:italic}"
        "li{margin:.5em 0}ul,ol{padding-left:1.3em}li p{margin:.35em 0}"
        "blockquote{border-left:3px solid #E2E8F0;margin:1em 0;padding:.1em 1.1em;color:#64748B}"
        "table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:.7em;border-bottom:1px solid #E2E8F0;overflow-wrap:anywhere}"
        "th{text-align:left;color:#64748B;font-weight:600}td{text-align:right;font-variant-numeric:tabular-nums}"
        "thead{background:#F5F7FC}.table-scroll{overflow:auto}.note{color:#64748B;font-size:14px}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}code{font-size:.9em}section{overflow-wrap:anywhere}"
        "@media(max-width:480px){body{padding:8px}main{padding:20px 16px}h2{font-size:21px}th,td{padding:.6em .4em}}"
    )
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'
        + escape(title)
        + '</title><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&amp;display=swap" rel="stylesheet"><style>'
        + style
        + "</style></head><body><main>"
        + body
        + "</main></body></html>"
    )
    return html, "\n\n".join(lines)


def _send(message: EmailMessage, config: SMTPConfig, password: str) -> DeliveryOutcome:
    client = None
    submitting = False
    accepted = False
    try:
        context = ssl.create_default_context()
        if config.tls_mode == "implicit":
            client = smtplib.SMTP_SSL(config.host, config.port, timeout=30, context=context)
        else:
            client = smtplib.SMTP(config.host, config.port, timeout=30)
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        if config.username:
            client.login(config.username, password)
        submitting = True
        refused = client.send_message(message, from_addr=config.sender, to_addrs=[config.recipient])
        if refused:
            return DeliveryOutcome(status="failed", message="The mail server refused the recipient.")
        accepted = True
        return DeliveryOutcome(status="sent", message="The mail server accepted the message.")
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
        return DeliveryOutcome(status="failed", message="The mail server rejected the message.")
    except (OSError, smtplib.SMTPException):
        return DeliveryOutcome(
            status="uncertain" if submitting and not accepted else "failed",
            message="Delivery acceptance is uncertain; check your inbox before resending."
            if submitting
            else "Unable to connect or authenticate securely. Check email settings.",
        )
    finally:
        if client is not None:
            # Closing locally avoids interpreting a QUIT failure as failed delivery.
            try:
                client.close()
            except OSError:
                pass


def send_report(
    result: Any,
    config: SMTPConfig,
    password: str,
    verified_recipient: str | None,
    timezone: str = "America/New_York",
) -> DeliveryOutcome:
    if not verified_recipient or config.recipient != verified_recipient:
        return DeliveryOutcome(
            status="failed", message="Verify the configured owner email address before sending reports."
        )
    html, plain = render_report(result, config.percent_only, timezone=timezone)
    message = EmailMessage()
    message["Subject"] = "Your Betafolio briefing"
    message["From"] = config.sender
    message["To"] = config.recipient
    message.set_content(plain)
    if not config.plain_text_only:
        message.add_alternative(html, subtype="html")
    return _send(message, config, password)


def send_verification(config: SMTPConfig, password: str, code: str) -> DeliveryOutcome:
    if not re.fullmatch(r"[A-Za-z0-9-]{6,64}", code):
        raise ValueError("Invalid verification code")
    message = EmailMessage()
    message["Subject"] = "Verify your portfolio briefing email"
    message["From"] = config.sender
    message["To"] = config.recipient
    message.set_content(
        f"Your verification code is: {code}\n\nEnter it in the dashboard to enable report delivery."
    )
    return _send(message, config, password)
