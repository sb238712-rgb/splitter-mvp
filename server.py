#!/usr/bin/env python3
"""Splitter MVP. Standard-library HTTP + SQLite; only QR image needs qrcode[pil].

This is a DEMO. No identity assurance, bank transactions, authorization/holds, or real payments.
Invitation links are bearer capabilities. Do not deploy as a financial product.
"""
import argparse
import io
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('SPLITTER_DB_PATH', ROOT / 'splitter_demo.sqlite3'))
TIMEOUT_SECONDS = 5 * 60
LOCK = threading.RLock()
PUBLIC_URL = None

PRODUCTS = {
    'pepperoni': {'name': 'Пепперони', 'emoji': '🍕', 'price_cents': 2490},
    'fourcheese': {'name': 'Четыре сыра', 'emoji': '🧀', 'price_cents': 2790},
    'margarita': {'name': 'Маргарита', 'emoji': '🍅', 'price_cents': 2190},
    'chicken': {'name': 'Курица BBQ', 'emoji': '🍗', 'price_cents': 2690},
    'fries': {'name': 'Картофель фри', 'emoji': '🍟', 'price_cents': 790},
    'nuggets': {'name': 'Наггетсы', 'emoji': '🥡', 'price_cents': 990},
    'lemonade': {'name': 'Лимонад 0,5 л', 'emoji': '🥤', 'price_cents': 490},
    'cola': {'name': 'Кола 0,5 л', 'emoji': '🥤', 'price_cents': 450},
}
DELIVERY_CENTS = 490
FREE_DELIVERY_FROM_CENTS = 6000


@contextmanager
def conn():
    db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        if db.in_transaction:
            db.commit()
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


def setup():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with conn() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
          CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, owner_token TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            status TEXT NOT NULL, total_cents INTEGER NOT NULL,
            owner_amount_cents INTEGER NOT NULL, owner_balance_cents INTEGER NOT NULL,
            cart_json TEXT NOT NULL, fail_reason TEXT NOT NULL DEFAULT '',
            ledger_json TEXT NOT NULL DEFAULT '[]'
          );
          CREATE TABLE IF NOT EXISTS invites (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            token TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            contact_masked TEXT NOT NULL, amount_cents INTEGER NOT NULL,
            status TEXT NOT NULL, balance_cents INTEGER NOT NULL DEFAULT 999999
          );
          CREATE INDEX IF NOT EXISTS idx_invites_session ON invites(session_id);
        ''')


def cart_from_rows(rows):
    if not isinstance(rows, list) or len(rows) > len(PRODUCTS):
        raise ValueError('В корзине слишком много разных товаров.')
    items, seen, subtotal = [], set(), 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Неверный товар.')
        sku, qty = row.get('sku'), row.get('qty')
        if sku not in PRODUCTS or type(qty) is not int or not 1 <= qty <= 20 or sku in seen:
            raise ValueError('Проверьте состав и количество товаров.')
        seen.add(sku)
        p = PRODUCTS[sku]
        items.append({'sku': sku, 'name': p['name'], 'emoji': p['emoji'],
                      'qty': qty, 'price_cents': p['price_cents']})
        subtotal += p['price_cents'] * qty
    if subtotal == 0:
        raise ValueError('Корзина пуста.')
    delivery = 0 if subtotal >= FREE_DELIVERY_FROM_CENTS else DELIVERY_CENTS
    return {'items': items, 'subtotal_cents': subtotal,
            'delivery_cents': delivery, 'total_cents': subtotal + delivery}


def validate_session(data):
    if not isinstance(data, dict):
        raise ValueError('Ожидается объект.')
    cart = cart_from_rows(data.get('items'))
    friends = data.get('participants')
    owner_amount = data.get('owner_amount_cents')
    owner_balance = data.get('owner_balance_cents', 50000)
    if not isinstance(friends, list) or not 1 <= len(friends) <= 8:
        raise ValueError('Добавьте от 1 до 8 друзей.')
    if type(owner_amount) is not int or owner_amount < 0:
        raise ValueError('Доля инициатора задана некорректно.')
    if type(owner_balance) is not int or not 0 <= owner_balance <= 10_000_000:
        raise ValueError('Некорректный тестовый баланс инициатора.')
    normalized, total = [], owner_amount
    for f in friends:
        if not isinstance(f, dict):
            raise ValueError('Неверные данные участника.')
        name = f.get('name')
        mask = f.get('contact_masked', '')
        amount = f.get('amount_cents')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
            raise ValueError('Укажите имя каждого друга.')
        if not isinstance(mask, str) or len(mask) > 50 or any(x.isdigit() for x in mask[:-4]):
            # A masked value may contain only the last four digits, never a full PAN/phone.
            raise ValueError('Не отправляйте полный номер карты или телефона.')
        if type(amount) is not int or amount <= 0:
            raise ValueError('Доля каждого друга должна быть больше нуля.')
        normalized.append({'name': name.strip(), 'contact_masked': mask, 'amount_cents': amount})
        total += amount
    if total != cart['total_cents']:
        raise ValueError('Сумма долей должна точно совпадать со стоимостью заказа.')
    return cart, normalized, owner_amount, owner_balance


def expire_if_needed(db, session):
    if session and session['status'] == 'pending' and int(time.time()) >= session['expires_at']:
        db.execute('UPDATE sessions SET status=?,fail_reason=? WHERE id=? AND status=?',
                   ('expired', 'Время подтверждения истекло. Вся операция отменена.', session['id'], 'pending'))
        db.execute('UPDATE invites SET status=? WHERE session_id=? AND status=?',
                   ('cancelled', session['id'], 'pending'))
        session = db.execute('SELECT * FROM sessions WHERE id=?', (session['id'],)).fetchone()
    return session


def get_session(db, owner_token):
    session = db.execute('SELECT * FROM sessions WHERE owner_token=?', (owner_token,)).fetchone()
    return expire_if_needed(db, session)


def session_data(db, s, include_tokens=True):
    friends = []
    for f in db.execute('SELECT * FROM invites WHERE session_id=? ORDER BY rowid', (s['id'],)):
        obj = {'id': f['id'], 'name': f['name'], 'contact_masked': f['contact_masked'],
               'amount_cents': f['amount_cents'], 'status': f['status']}
        if include_tokens:
            obj['token'] = f['token']
            obj['invite_url'] = f'{PUBLIC_URL}/friend/{urllib.parse.quote(f["token"])}'
        friends.append(obj)
    return {'id': s['id'], 'status': s['status'], 'total_cents': s['total_cents'],
            'owner_amount_cents': s['owner_amount_cents'], 'cart': json.loads(s['cart_json']),
            'participants': friends, 'expires_at': s['expires_at'] * 1000,
            'reason': s['fail_reason'], 'ledger': json.loads(s['ledger_json'])}


def create_session(data):
    cart, friends, owner_amount, owner_balance = validate_session(data)
    now = int(time.time())
    session_id = 'SPL-' + secrets.token_hex(4).upper()
    owner_token = secrets.token_urlsafe(28)
    with LOCK, conn() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''INSERT INTO sessions
            (id,owner_token,created_at,expires_at,status,total_cents,
             owner_amount_cents,owner_balance_cents,cart_json)
             VALUES(?,?,?,?,?,?,?,?,?)''',
             (session_id,owner_token,now,now+TIMEOUT_SECONDS,'pending',cart['total_cents'],
              owner_amount,owner_balance,json.dumps(cart,ensure_ascii=False)))
        for f in friends:
            db.execute('''INSERT INTO invites
                (id,session_id,token,name,contact_masked,amount_cents,status)
                VALUES(?,?,?,?,?,?,?)''',
                (secrets.token_hex(9), session_id, secrets.token_urlsafe(28),
                 f['name'],f['contact_masked'],f['amount_cents'],'pending'))
        s = get_session(db, owner_token)
        result = session_data(db,s)
        result['owner_token'] = owner_token
        db.commit()
        return result


def public_invite(db, token):
    f = db.execute('SELECT * FROM invites WHERE token=?', (token,)).fetchone()
    if not f:
        return None
    s = expire_if_needed(db, db.execute('SELECT * FROM sessions WHERE id=?', (f['session_id'],)).fetchone())
    f = db.execute('SELECT * FROM invites WHERE token=?', (token,)).fetchone()
    return {'name': f['name'], 'amount_cents': f['amount_cents'],
            'status': f['status'], 'session_status': s['status'], 'reason': s['fail_reason'],
            'expires_at': s['expires_at'] * 1000, 'session_id': s['id']}


def respond(token, decision):
    if decision not in ('approve','reject','insufficient'):
        raise ValueError('Неизвестный ответ.')
    with LOCK, conn() as db:
        db.execute('BEGIN IMMEDIATE')
        f = db.execute('SELECT * FROM invites WHERE token=?', (token,)).fetchone()
        if not f:
            raise LookupError('Приглашение не найдено.')
        s = expire_if_needed(db, db.execute('SELECT * FROM sessions WHERE id=?', (f['session_id'],)).fetchone())
        f = db.execute('SELECT * FROM invites WHERE token=?', (token,)).fetchone()
        if s['status'] != 'pending' or f['status'] != 'pending':
            result = public_invite(db,token)
            db.commit()
            return result
        if decision in ('reject','insufficient'):
            label = ('отклонил запрос' if decision == 'reject' else 'не прошёл демо-проверку средств')
            reason = f'Участник «{f["name"]}» {label}. Весь заказ отменён.'
            db.execute('UPDATE sessions SET status=?,fail_reason=? WHERE id=?', ('cancelled',reason,s['id']))
            db.execute('UPDATE invites SET status=? WHERE token=?',
                       ('rejected' if decision=='reject' else 'insufficient',token))
            db.execute('UPDATE invites SET status=? WHERE session_id=? AND status=?',
                       ('cancelled',s['id'],'pending'))
        else:
            db.execute('UPDATE invites SET status=? WHERE token=?', ('approved',token))
            all_f = db.execute('SELECT * FROM invites WHERE session_id=?', (s['id'],)).fetchall()
            if all(f2['status']=='approved' for f2 in all_f):
                if s['owner_balance_cents'] < s['owner_amount_cents']:
                    db.execute('UPDATE sessions SET status=?,fail_reason=? WHERE id=?',
                               ('cancelled','У инициатора недостаточно тестовых средств. Все доли отменены.',s['id']))
                else:
                    # Simulation only; no external transfers occur. One DB transaction, all-or-none state.
                    ledger = [{'label':f'От {f2["name"]} → инициатору', 'amount_cents':f2['amount_cents']}
                              for f2 in all_f]
                    ledger.append({'label':'От инициатора → Pizzaro','amount_cents':s['total_cents']})
                    db.execute('UPDATE sessions SET status=?, ledger_json=? WHERE id=?',
                               ('success',json.dumps(ledger,ensure_ascii=False),s['id']))
        result = public_invite(db,token)
        db.commit()
        return result


def cancel_owner(owner_token):
    with LOCK,conn() as db:
        db.execute('BEGIN IMMEDIATE')
        s = get_session(db,owner_token)
        if not s:
            raise LookupError('Сеанс не найден.')
        if s['status']=='pending':
            db.execute('UPDATE sessions SET status=?,fail_reason=? WHERE id=?',
                       ('cancelled','Инициатор отменил сбор платежей.',s['id']))
            db.execute('UPDATE invites SET status=? WHERE session_id=? AND status=?',
                       ('cancelled',s['id'],'pending'))
        result = session_data(db,db.execute('SELECT * FROM sessions WHERE id=?',(s['id'],)).fetchone())
        db.commit()
        return result


def detect_lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('192.0.2.1', 80))
        ip = sock.getsockname()[0]
        return ip if not ip.startswith('127.') else 'localhost'
    except OSError:
        return 'localhost'
    finally:
        sock.close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'SplitterDemo/2.0'
    def log_message(self, fmt, *args):
        print('%s %s' % (self.address_string(), fmt % args), flush=True)

    def response_headers(self, content_type, status=200, length=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        if length is not None: self.send_header('Content-Length',str(length))
        self.end_headers()

    def json(self,obj,status=200):
        raw=json.dumps(obj,ensure_ascii=False).encode('utf-8')
        self.response_headers('application/json; charset=utf-8',status,len(raw))
        self.wfile.write(raw)

    def html(self,filename):
        raw=(ROOT / filename).read_bytes()
        self.response_headers('text/html; charset=utf-8',200,len(raw))
        self.wfile.write(raw)

    def do_GET(self):
        path=urllib.parse.urlsplit(self.path).path
        try:
            if path in ('/','/index.html'):
                return self.html('index.html')
            if path=='/friend.html' or path.startswith('/friend/'):
                return self.html('friend.html')
            if path=='/api/health':
                return self.json({'ok':True,'qr_enabled':qr_available(),
                                  'public_url':PUBLIC_URL,'currency':'BYN'})
            if path.startswith('/api/sessions/') and '/' not in path[len('/api/sessions/'):]:
                token=path.split('/')[-1]
                with LOCK,conn() as db:
                    s=get_session(db,token)
                    if not s: return self.json({'error':'Сеанс не найден.'},404)
                    return self.json(session_data(db,s))
            if path.startswith('/api/invite/') and '/' not in path[len('/api/invite/'):]:
                token=path.split('/')[-1]
                with LOCK,conn() as db:
                    res=public_invite(db,token)
                    if not res:return self.json({'error':'Приглашение не найдено.'},404)
                    return self.json(res)
            if path.startswith('/api/qr/') and '/' not in path[len('/api/qr/'):]:
                token=path.split('/')[-1]
                with conn() as db:
                    if not db.execute('SELECT 1 FROM invites WHERE token=?',(token,)).fetchone():
                        return self.json({'error':'Приглашение не найдено.'},404)
                if not qr_available():
                    return self.json({'error':'Для QR установите зависимости: pip install -r requirements.txt'},503)
                import qrcode
                code=qrcode.QRCode(version=None,box_size=9,border=3,
                                   error_correction=qrcode.constants.ERROR_CORRECT_M)
                code.add_data(f'{PUBLIC_URL}/friend/{urllib.parse.quote(token)}')
                code.make(fit=True)
                with io.BytesIO() as buffer:
                    code.make_image(fill_color='#241a3b',back_color='#ffffff').save(buffer,format='PNG')
                    raw=buffer.getvalue()
                self.response_headers('image/png',200,len(raw))
                self.wfile.write(raw)
                return
            return self.json({'error':'Не найдено.'},404)
        except (ValueError,LookupError) as e:
            self.json({'error':str(e)},400)
        except Exception as e:
            print('GET error',type(e).__name__,str(e),flush=True)
            self.json({'error':'Внутренняя ошибка сервера.'},500)

    def do_POST(self):
        path=urllib.parse.urlsplit(self.path).path
        if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
            return self.json({'error':'Требуется application/json.'},415)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length < 2 or length > 25000:
                return self.json({'error':'Некорректный размер запроса.'},400)
            body=json.loads(self.rfile.read(length))
            if path=='/api/sessions':
                return self.json(create_session(body),201)
            if path.startswith('/api/invite/') and path.endswith('/respond'):
                token=path.removeprefix('/api/invite/').removesuffix('/respond').rstrip('/')
                return self.json(respond(token,body.get('decision')))
            if path.startswith('/api/sessions/') and path.endswith('/cancel'):
                token=path.removeprefix('/api/sessions/').removesuffix('/cancel').rstrip('/')
                return self.json(cancel_owner(token))
            return self.json({'error':'Не найдено.'},404)
        except (ValueError,TypeError,json.JSONDecodeError) as e:
            return self.json({'error':str(e) or 'Некорректный JSON.'},400)
        except LookupError as e:
            return self.json({'error':str(e)},404)
        except Exception as e:
            print('POST error',type(e).__name__,str(e),flush=True)
            return self.json({'error':'Внутренняя ошибка сервера.'},500)


def qr_available():
    try:
        import qrcode  # noqa:F401
        import PIL  # noqa:F401
        return True
    except ImportError:
        return False


def main():
    global PUBLIC_URL
    parser=argparse.ArgumentParser(description='Splitter local/demo QR backend')
    parser.add_argument('--host',default='0.0.0.0')
    parser.add_argument('--port',type=int,default=int(os.environ.get('PORT', '8765')))
    parser.add_argument('--public-url',default=os.environ.get('SPLITTER_PUBLIC_URL') or os.environ.get('RENDER_EXTERNAL_URL'))
    args=parser.parse_args()
    if args.public_url:
        PUBLIC_URL=args.public_url.rstrip('/')
    else:
        PUBLIC_URL=f'http://{detect_lan_ip()}:{args.port}'
    setup()
    httpd=ThreadingHTTPServer((args.host,args.port),Handler)
    print(f'Splitter MVP · открыть на компьютере: http://localhost:{args.port}',flush=True)
    print(f'Ссылка для телефона (в одной сети): {PUBLIC_URL}',flush=True)
    print(f'QR доступен: {"да" if qr_available() else "нет, выполните pip install -r requirements.txt"}',flush=True)
    try:httpd.serve_forever()
    except KeyboardInterrupt:pass
    finally:httpd.server_close()

if __name__=='__main__':
    main()
