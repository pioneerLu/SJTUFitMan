"""Keep bounded website error details without retaining response payloads or identities."""
import html
import re


def identity_redactions(value):
    """Known personal identifiers supplement generic token/email/number redaction."""
    result = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and item and any(part in key.lower() for part in (
                    'name', 'account', 'password', 'token', 'cookie', 'email',
                    'phone', 'mobile', 'number', 'student', 'userid', 'sender', 'recipient')):
                result.append(item)
            elif isinstance(item, (dict, list)):
                result.extend(identity_redactions(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(identity_redactions(item))
    return result


def confirm_feedback(status, body, redactions=()):
    """Only HTTP status, integer business code and a scrubbed failure message escape."""
    result = {'http_status': status}
    if not isinstance(body, dict):
        return result
    code = body.get('code')
    if type(code) is int:
        result['code'] = code
    if status == 200 and type(code) is int and code == 0:
        return result
    message = body.get('msg') or body.get('message')
    if not isinstance(message, str):
        return result
    # Cap before processing as well as after processing; never export data/headers.
    message = html.unescape(re.sub(r'<[^>]*>', '', message[:2000]))
    for private in sorted(set(redactions), key=len, reverse=True):
        if private:
            message = message.replace(private, '[已隐藏]')
    message = re.sub(r'(?i)(?:https?://|www\.)\S+', '[链接已隐藏]', message)
    message = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[邮箱已隐藏]', message)
    message = re.sub(r'(?i)\b(?:token|authorization|cookie|password|sid|tid|密码|授权码)\s*[:=：]\s*\S+', '[已隐藏]', message)
    message = re.sub(r'[A-Za-z0-9_+/=-]{16,}|\b\d{6,}\b', '[已隐藏]', message)
    message = re.sub(r'[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]', ' ', message)
    message = ' '.join(message.split())[:240]
    if message:
        result['message'] = message
    return result


def failure_detail(result):
    """Shared plain text for desktop and polling status; old records remain readable."""
    feedback = result.get('server_feedback')
    if not isinstance(feedback, dict):
        return ''
    lines = []
    if isinstance(feedback.get('message'), str) and feedback['message']:
        lines.append('网站提示：' + feedback['message'])
    numbers = []
    if type(feedback.get('http_status')) is int:
        numbers.append('HTTP ' + str(feedback['http_status']))
    if type(feedback.get('code')) is int:
        numbers.append('错误码 ' + str(feedback['code']))
    if numbers:
        lines.append('网站返回：' + '，'.join(numbers))
    return '\n'.join(lines)
