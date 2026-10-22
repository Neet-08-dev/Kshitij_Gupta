"""Runtime settings come from environment variables. A local .env is read only as a development convenience."""
import os
import ssl
from pathlib import Path


def _load_dotenv():
    path = Path(__file__).resolve().parents[1] / '.env'
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def settings():
    """Read on every request so operators can flip MODEL_DISABLED without a rebuild."""
    env = os.environ
    return {
        'api_key': env.get('MODEL_API_KEY', ''),
        'base_url': env.get('MODEL_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/openai').rstrip('/'),
        'model': env.get('MODEL_NAME', 'gemini-3.1-flash-lite'),
        'disabled': env.get('MODEL_DISABLED', '').lower() in {'1', 'true', 'yes'},
        'reasoning': env.get('MODEL_REASONING', '').lower() in {'1', 'true', 'yes'},
        'provider_sort': env.get('MODEL_PROVIDER_SORT', 'throughput'),  # OpenRouter only; empty string disables it
        'model_timeout_s': float(env.get('MODEL_TIMEOUT_S', '12')),
        'request_budget_s': float(env.get('REQUEST_BUDGET_S', '50')),
        # USD per million tokens, used only when the provider reports no per-call cost (Gemini's OpenAI endpoint)
        'price_in_per_m': float(env.get('MODEL_PRICE_IN_PER_M') or 0),
        'price_out_per_m': float(env.get('MODEL_PRICE_OUT_PER_M') or 0),
    }


def ssl_context():
    """python.org macOS builds ship without a CA bundle; fall back to the system bundle when needed."""
    context = ssl.create_default_context()
    if not os.environ.get('SSL_CERT_FILE') and not ssl.get_default_verify_paths().cafile and os.path.exists('/etc/ssl/cert.pem'):
        context.load_verify_locations('/etc/ssl/cert.pem')
    return context
