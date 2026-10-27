"""Provider configuration and ordered fallback for Gemini and OpenAI-compatible APIs.
Keys are stored in a private JSON file (0600); API responses never return key values.
"""
import json, os, re, time, urllib.request, urllib.error, socket
from pathlib import Path
import config
import gemini_client

ROOT = Path(__file__).resolve().parent.parent
STORE = config.DATA_DIR / "providers.json"

class ProviderError(Exception): pass

def _read():
    try:
        obj=json.loads(STORE.read_text(encoding="utf-8"))
        return obj if isinstance(obj,dict) else {"providers":[],"order":[]}
    except (FileNotFoundError, ValueError):
        return {"providers":[],"order":[]}

def _write(obj):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp=STORE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    try: os.chmod(tmp,0o600)
    except OSError: pass
    tmp.replace(STORE)
    try: os.chmod(STORE,0o600)
    except OSError: pass

def public_list():
    data=_read(); out=[]
    for x in data.get('providers',[]):
        out.append({"id":x.get('id'),"name":x.get('name'),"provider":x.get('provider'),"model":x.get('model'),"base_url":x.get('base_url',''),"enabled":bool(x.get('enabled',True)),"has_key":bool(x.get('api_key'))})
    return {"providers":out,"order":[i for i in data.get('order',[]) if any(x.get('id')==i and x.get('enabled',True) for x in data.get('providers',[]))]}

def save(data):
    kind=str(data.get('provider') or 'gemini').lower().strip()
    if kind not in ('gemini','openai_compatible'): raise ValueError('Provider must be Gemini or OpenAI-compatible.')
    name=str(data.get('name') or kind.title()).strip()[:60]
    model=str(data.get('model') or ('auto' if kind=='gemini' else '')).strip()[:120]
    api_key=str(data.get('api_key') or '').strip()
    base=str(data.get('base_url') or '').strip().rstrip('/')
    if not name: raise ValueError('Enter a provider name.')
    if kind=='gemini':
        if api_key and not gemini_client.valid_key_format(api_key): raise ValueError('Gemini key format is not valid.')
        if model!='auto' and model and not gemini_client.valid_model_name(model): raise ValueError('Gemini model name is not valid.')
        base=''
    else:
        if not base.startswith('https://') or len(base)>300: raise ValueError('OpenAI-compatible base URL must start with https://.')
        if not model: raise ValueError('Enter the model name supported by your API provider.')
        if not api_key and not data.get('id'): raise ValueError('Enter an API key.')
    store=_read(); pid=str(data.get('id') or ('p'+str(int(time.time()*1000))))
    old=next((x for x in store['providers'] if x.get('id')==pid),None)
    if old and not api_key: api_key=old.get('api_key','')
    entry={"id":pid,"name":name,"provider":kind,"model":model,"base_url":base,"api_key":api_key,"enabled":bool(data.get('enabled',True))}
    if old: store['providers']=[entry if x.get('id')==pid else x for x in store['providers']]
    else: store.setdefault('providers',[]).append(entry)
    order=store.setdefault('order',[])
    if pid not in order: order.append(pid)
    _write(store); return public_list()

def remove(pid):
    d=_read(); d['providers']=[x for x in d.get('providers',[]) if x.get('id')!=pid]; d['order']=[x for x in d.get('order',[]) if x!=pid]; _write(d); return public_list()

def reorder(order):
    d=_read(); valid={x.get('id') for x in d.get('providers',[]) if x.get('enabled',True)}
    d['order']=[x for x in order if x in valid]+[x for x in d.get('order',[]) if x in valid and x not in order]
    _write(d); return public_list()

def _gemini_call(provider, system_prompt, history, timeout=30):
    """One Gemini provider entry. Uses the shared client so the Astra identity rules,
    model auto-detection and retry logic apply to every Gemini entry in the list."""
    key = provider.get('api_key', '')
    if not key:
        raise ProviderError('API key is not configured for ' + provider.get('name', 'Gemini'))
    setting = (provider.get('model') or 'auto').strip()
    try:
        return gemini_client.generate(setting, system_prompt, history, max_tokens=1200, timeout=timeout, api_key=key)
    except gemini_client.GeminiError as e:
        raise ProviderError('%s: %s' % (provider.get('name', 'Gemini'), str(e)[:200]))


def _openai_call(provider, system_prompt, history, timeout=30):
    key = provider.get('api_key', '')
    if not key:
        raise ProviderError('API key is not configured for ' + provider.get('name', 'provider'))
    prompt = ((system_prompt or '').strip()[:5000] + '\n\n' + gemini_client.CORE_PERSONA)[:7000]
    messages = [{"role": "system", "content": prompt}]
    for direction, txt in history:
        txt = str(txt or '').strip()[:2000]
        if txt:
            messages.append({"role": "user" if direction == 'in' else "assistant", "content": txt})
    if messages[-1]['role'] != 'user':
        raise ProviderError('No incoming message to answer.')
    body = json.dumps({"model": provider['model'], "messages": messages, "max_tokens": 1200, "temperature": 0.7}).encode()
    req = urllib.request.Request(provider['base_url'] + '/chat/completions', data=body, method='POST',
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read().decode('utf-8'))
        text = result['choices'][0]['message']['content']
        if isinstance(text, list):
            text = ''.join(str(x.get('text', '')) for x in text if isinstance(x, dict))
        text = str(text or '').strip()
        if not text:
            raise ProviderError(provider['name'] + ' returned an empty response.')
        return text[:1500], provider['model']
    except urllib.error.HTTPError as e:
        code = e.code
        if code in (408, 429) or code >= 500:
            raise ProviderError('%s temporary API error (HTTP %d); trying fallback.' % (provider['name'], code))
        raise ProviderError('%s request rejected (HTTP %d); check its key, model and endpoint.' % (provider['name'], code))
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        raise ProviderError(provider['name'] + ' network/timeout error; trying fallback.')
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProviderError(provider['name'] + ' returned an invalid response.')


def _ordered():
    d = _read()
    byid = {x.get('id'): x for x in d.get('providers', []) if x.get('enabled', True)}
    ordered = [byid[i] for i in d.get('order', []) if i in byid]
    ordered += [x for x in byid.values() if x not in ordered]
    return ordered


def generate(setting, system_prompt, history):
    ordered = _ordered()
    # Keep the original single-key Gemini setup working when no provider entries exist.
    if not ordered and gemini_client.configured():
        return gemini_client.generate(setting, system_prompt, history)
    errors = []
    for x in ordered:
        try:
            if x['provider'] == 'gemini':
                return _gemini_call(x, system_prompt, history, timeout=30)
            return _openai_call(x, system_prompt, history)
        except Exception as e:
            errors.append('%s: %s' % (x.get('name', 'provider'), str(e)[:180]))
    if not ordered:
        raise ProviderError('No enabled AI provider is configured. Add one under AI & API.')
    raise ProviderError('All AI providers failed. ' + ' | '.join(errors[-4:]))


def generate_audio(setting, system_prompt, history, audio_base64, mime_type='audio/ogg', reply_mode='same'):
    """Voice notes need a Gemini key (OpenAI-compatible chat endpoints cannot hear audio)."""
    errors = []
    for x in _ordered():
        if x['provider'] != 'gemini' or not x.get('api_key'):
            continue
        try:
            return gemini_client.generate_audio((x.get('model') or 'auto'), system_prompt, history,
                                                audio_base64, mime_type, api_key=x['api_key'], reply_mode=reply_mode)
        except gemini_client.GeminiError as e:
            errors.append('%s: %s' % (x.get('name', 'Gemini'), str(e)[:160]))
    if gemini_client.configured():
        return gemini_client.generate_audio(setting, system_prompt, history, audio_base64, mime_type, reply_mode=reply_mode)
    if errors:
        raise ProviderError('Voice note failed. ' + ' | '.join(errors[-3:]))
    raise ProviderError('Voice notes need a Google Gemini API key. Add one under AI & API.')


def any_ai_configured():
    return bool(_ordered()) or gemini_client.configured()


def test(pid):
    d=_read(); x=next((z for z in d.get('providers',[]) if z.get('id')==pid),None)
    if not x: raise ValueError('Provider not found.')
    if x['provider']=='gemini':
        text,model=_gemini_call(x,'Answer with one short word.',[('in','Say OK.')],timeout=20)
        return {"ok":True,"model":model}
    text,model=_openai_call(x,'Answer with one short word.',[('in','Say OK.')],timeout=20)
    return {"ok":True,"model":model}
