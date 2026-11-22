#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Loja de programas: catalogo, area do cliente (login) e downloads protegidos.
Python 3.9+, so biblioteca padrao + SQLite. Compra manual (Pix/WhatsApp): voce libera o cliente no /admin."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

AQUI = Path(__file__).resolve().parent
DB_PATH = os.environ.get("LOJA_DB", str(AQUI / "dados" / "loja.db"))
DATA_DIR = Path(DB_PATH).resolve().parent
ARQUIVOS = DATA_DIR / "arquivos"
PORT = int(os.environ.get("PORT", "8082"))
HOST = os.environ.get("HOST", "0.0.0.0")
ADMIN_SENHA = os.environ.get("ADMIN_SENHA", "")
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "500")) * 1024 * 1024
SESSAO_DIAS = 30
PBKDF2_ITER = 200_000
LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, msg):
        self.status, self.msg = status, msg


def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("pragma foreign_keys=on")
    return c


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARQUIVOS.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.execute("pragma journal_mode=wal")
        c.executescript("""
        create table if not exists produtos(
          id integer primary key autoincrement, slug text unique not null, nome text not null, icone text default '📦',
          resumo text default '', descricao text default '', preco text default '', ativo integer default 1,
          ordem integer default 0, criado text);
        create table if not exists arquivos(
          id integer primary key autoincrement, produto_id integer not null references produtos(id) on delete cascade,
          nome text not null, guardado text not null, versao text default '', plataforma text default 'Windows',
          notas text default '', tamanho integer default 0, sha256 text default '', ativo integer default 1, criado text);
        create table if not exists clientes(
          id integer primary key autoincrement, email text unique not null, nome text default '', whatsapp text default '',
          sal text not null, hash text not null, ativo integer default 1, criado text);
        create table if not exists acessos(
          id integer primary key autoincrement, cliente_id integer not null references clientes(id) on delete cascade,
          produto_id integer not null references produtos(id) on delete cascade,
          chave text default '', obs text default '', ate text default '', ativo integer default 1, criado text,
          unique(cliente_id, produto_id));
        create table if not exists sessoes(
          token text primary key, cliente_id integer not null references clientes(id) on delete cascade, expira real);
        create table if not exists downloads(
          id integer primary key autoincrement, cliente_id integer, arquivo_id integer, quando text, ip text);
        create table if not exists config(chave text primary key, valor text);
        """)


def agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


CFG_PADRAO = {"nome_loja": "Minha Loja de Programas", "whatsapp": "", "descricao": "Programas prontos para o seu negócio.",
              "rodape": ""}


def get_cfg(c=None):
    own = c is None
    c = c or conn()
    out = dict(CFG_PADRAO)
    for r in c.execute("select chave,valor from config"):
        if r["chave"] in out:
            out[r["chave"]] = r["valor"]
    if own:
        c.close()
    return out


# ---------- senhas / sessoes ----------
def hash_senha(senha, sal=None):
    sal = sal or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), bytes.fromhex(sal), PBKDF2_ITER).hex()
    return sal, h


def confere_senha(senha, sal, h):
    return hmac.compare_digest(hash_senha(senha, sal)[1], h)


def senha_aleatoria():
    alfa = "abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRTUVWXYZ23456789"
    return "".join(secrets.choice(alfa) for _ in range(10))


def tok_hash(t):
    return hashlib.sha256(t.encode()).hexdigest()


_RL = {}


def rate_ok(chave, limite, janela):
    t = time.time()
    with LOCK:
        L = [x for x in _RL.get(chave, []) if t - x < janela]
        if len(L) >= limite:
            _RL[chave] = L
            return False
        L.append(t)
        _RL[chave] = L
    return True


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def email_ok(e):
    e = str(e or "").strip().lower()
    if not EMAIL_RE.match(e) or len(e) > 120:
        raise ApiError(400, "E-mail inválido")
    return e


# ---------- infra de rotas ----------
ROTAS = []


def route(metodo, padrao, admin=False, stream=False):
    def deco(fn):
        ROTAS.append((metodo, re.compile("^" + padrao + "$"), fn, admin, stream))
        return fn
    return deco


class Raw:
    def __init__(self, corpo, status=200, ctype="text/html; charset=utf-8", headers=None):
        self.corpo = corpo if isinstance(corpo, bytes) else corpo.encode("utf-8")
        self.status, self.ctype, self.headers = status, ctype, headers or {}


class Stream:
    def __init__(self, caminho, nome, ctype="application/octet-stream"):
        self.caminho, self.nome, self.ctype = caminho, nome, ctype


class Req:
    def __init__(self, h, args, body, q):
        self.h, self.args, self.body, self.q = h, args, body, q
        self.ip = h.client_address[0]
        if os.environ.get("TRUST_PROXY") == "1":
            self.ip = h.headers.get("X-Forwarded-For", self.ip).split(",")[0].strip()
        self._cli = False

    def cliente(self, obrigatorio=True):
        if self._cli is False:
            self._cli = None
            ck = SimpleCookie(self.h.headers.get("Cookie", ""))
            if "sid" in ck:
                with conn() as c:
                    r = c.execute("select c.* from sessoes s join clientes c on c.id=s.cliente_id "
                                  "where s.token=? and s.expira>? and c.ativo=1", (tok_hash(ck["sid"].value), time.time())).fetchone()
                self._cli = r
        if obrigatorio and not self._cli:
            raise ApiError(401, "Entre na sua conta para continuar")
        return self._cli


def pagina(nome):
    return Raw((AQUI / nome).read_text(encoding="utf-8"))


def slug(t):
    t = str(t).lower()
    for a, b in (("ç", "c"), ("ã", "a"), ("õ", "o")):
        t = t.replace(a, b)
    t = "".join(ch for ch in __import__("unicodedata").normalize("NFD", t) if __import__("unicodedata").category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:40]


def tem_acesso_ativo(a):
    if not a["ativo"]:
        return False
    return not a["ate"] or a["ate"] >= date.today().isoformat()


# ---------- paginas ----------
@route("GET", "/")
def p_home(r):
    return pagina("index.html")


@route("GET", "/admin", admin=True)
def p_admin(r):
    return pagina("admin.html")


# ---------- publico ----------
@route("GET", "/api/publico")
def api_publico(r):
    with conn() as c:
        cfg = get_cfg(c)
        prods = []
        for p in c.execute("select * from produtos where ativo=1 order by ordem, id"):
            d = dict(p)
            v = c.execute("select versao from arquivos where produto_id=? and ativo=1 order by id desc limit 1", (p["id"],)).fetchone()
            d["versao"] = v["versao"] if v else ""
            prods.append(d)
    wa = re.sub(r"\D", "", cfg["whatsapp"])
    for p in prods:
        p["comprar"] = (f"https://wa.me/{wa}?text=" + quote(f"Olá! Quero comprar o {p['nome']}.")) if wa else ""
    cfg["whatsapp"] = wa
    return {"loja": cfg, "produtos": prods}


@route("POST", "/api/login")
def api_login(r):
    email = str(r.body.get("email") or "").strip().lower()
    senha = str(r.body.get("senha") or "")
    if not rate_ok("login|" + r.ip, 15, 900) or not rate_ok("login|" + email, 8, 900):
        raise ApiError(429, "Muitas tentativas. Aguarde alguns minutos.")
    with conn() as c:
        cl = c.execute("select * from clientes where email=? and ativo=1", (email,)).fetchone()
        # compara sempre (evita revelar se o e-mail existe pelo tempo de resposta)
        sal, h = (cl["sal"], cl["hash"]) if cl else ("00" * 16, "0" * 64)
        ok = confere_senha(senha, sal, h) and cl is not None
        if not ok:
            raise ApiError(401, "E-mail ou senha incorretos")
        token = secrets.token_urlsafe(32)
        c.execute("delete from sessoes where expira<?", (time.time(),))
        c.execute("insert into sessoes(token,cliente_id,expira) values(?,?,?)",
                  (tok_hash(token), cl["id"], time.time() + SESSAO_DIAS * 86400))
    seguro = "; Secure" if r.h.headers.get("X-Forwarded-Proto") == "https" else ""
    ck = f"sid={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSAO_DIAS * 86400}{seguro}"
    return Raw(json.dumps({"ok": True, "nome": cl["nome"]}), 200, "application/json", {"Set-Cookie": ck})


@route("POST", "/api/logout")
def api_logout(r):
    ck = SimpleCookie(r.h.headers.get("Cookie", ""))
    if "sid" in ck:
        with conn() as c:
            c.execute("delete from sessoes where token=?", (tok_hash(ck["sid"].value),))
    return Raw(json.dumps({"ok": True}), 200, "application/json", {"Set-Cookie": "sid=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})


@route("GET", "/api/conta")
def api_conta(r):
    cl = r.cliente(False)
    if not cl:
        return {"logado": False}
    with conn() as c:
        acessos = []
        for a in c.execute("select a.*, p.nome pnome, p.icone, p.slug from acessos a join produtos p on p.id=a.produto_id "
                           "where a.cliente_id=? and p.ativo=1 order by a.id", (cl["id"],)):
            ativo = tem_acesso_ativo(a)
            arqs = []
            if ativo:
                arqs = [dict(x) for x in c.execute(
                    "select id,nome,versao,plataforma,notas,tamanho,sha256,criado from arquivos where produto_id=? and ativo=1 order by id desc",
                    (a["produto_id"],))]
            acessos.append({"produto": a["pnome"], "icone": a["icone"], "chave": a["chave"] if ativo else "", "ate": a["ate"],
                            "obs": a["obs"], "ativo": ativo, "arquivos": arqs})
        cfg = get_cfg(c)
    return {"logado": True, "nome": cl["nome"], "email": cl["email"], "acessos": acessos,
            "whatsapp": re.sub(r"\D", "", cfg["whatsapp"])}


@route("POST", "/api/conta/senha")
def api_trocar_senha(r):
    cl = r.cliente()
    atual, nova = str(r.body.get("atual") or ""), str(r.body.get("nova") or "")
    if len(nova) < 8:
        raise ApiError(400, "A nova senha precisa ter pelo menos 8 caracteres")
    if not confere_senha(atual, cl["sal"], cl["hash"]):
        raise ApiError(400, "Senha atual incorreta")
    sal, h = hash_senha(nova)
    with conn() as c:
        c.execute("update clientes set sal=?, hash=? where id=?", (sal, h, cl["id"]))
    return {"ok": True}


@route("GET", r"/download/(\d+)")
def baixar(r):
    cl = r.cliente()
    with conn() as c:
        f = c.execute("select * from arquivos where id=? and ativo=1", (int(r.args[0]),)).fetchone()
        ac = c.execute("select * from acessos where cliente_id=? and produto_id=?", (cl["id"], f["produto_id"])).fetchone() if f else None
        if not f or not ac or not tem_acesso_ativo(ac):
            raise ApiError(403, "Você não tem acesso a este arquivo")
        c.execute("insert into downloads(cliente_id,arquivo_id,quando,ip) values(?,?,?,?)", (cl["id"], f["id"], agora(), r.ip))
    p = ARQUIVOS / f["guardado"]
    if not p.exists():
        raise ApiError(404, "Arquivo indisponível. Fale com o vendedor.")
    return Stream(p, f["nome"])


# ---------- admin ----------
def exige_json_admin():
    pass


@route("GET", "/api/admin/resumo", admin=True)
def adm_resumo(r):
    with conn() as c:
        return {"produtos": [dict(p, arquivos=[dict(a) for a in c.execute(
                    "select * from arquivos where produto_id=? and ativo=1 order by id desc", (p["id"],))])
                    for p in c.execute("select * from produtos order by ordem,id")],
                "clientes": [dict(x) for x in c.execute(
                    "select c.id,c.email,c.nome,c.whatsapp,c.ativo,c.criado,(select count(*) from downloads d where d.cliente_id=c.id) baixou "
                    "from clientes c order by c.id desc")],
                "acessos": [dict(x) for x in c.execute(
                    "select a.*, p.nome pnome from acessos a join produtos p on p.id=a.produto_id order by a.id desc")],
                "downloads": [dict(x) for x in c.execute(
                    "select d.quando,d.ip,c.email,a.nome arquivo,a.versao from downloads d left join clientes c on c.id=d.cliente_id "
                    "left join arquivos a on a.id=d.arquivo_id order by d.id desc limit 50")],
                "config": get_cfg(c), "url": f"http://{r.h.headers.get('Host', 'localhost')}"}


def vals_produto(b):
    nome = str(b.get("nome") or "").strip()[:80]
    if not nome:
        raise ApiError(400, "Dê um nome ao programa")
    return (nome, str(b.get("icone") or "📦").strip()[:8], str(b.get("resumo") or "").strip()[:200],
            str(b.get("descricao") or "").strip()[:4000], str(b.get("preco") or "").strip()[:80], int(bool(b.get("ativo", True))))


@route("POST", "/api/admin/produtos", admin=True)
def adm_prod_novo(r):
    v = vals_produto(r.body)
    base = slug(v[0]) or "programa"
    with conn() as c:
        s, n = base, 1
        while c.execute("select 1 from produtos where slug=?", (s,)).fetchone():
            n += 1
            s = f"{base}-{n}"
        ordem = c.execute("select coalesce(max(ordem),0)+1 from produtos").fetchone()[0]
        cur = c.execute("insert into produtos(slug,nome,icone,resumo,descricao,preco,ativo,ordem,criado) values(?,?,?,?,?,?,?,?,?)",
                        (s,) + v + (ordem, agora()))
        return {"id": cur.lastrowid}


@route("PUT", r"/api/admin/produtos/(\d+)", admin=True)
def adm_prod_edita(r):
    v = vals_produto(r.body)
    with conn() as c:
        c.execute("update produtos set nome=?,icone=?,resumo=?,descricao=?,preco=?,ativo=? where id=?", v + (int(r.args[0]),))
    return {"ok": True}


@route("DELETE", r"/api/admin/produtos/(\d+)", admin=True)
def adm_prod_apaga(r):
    with conn() as c:
        for a in c.execute("select guardado from arquivos where produto_id=?", (int(r.args[0]),)):
            (ARQUIVOS / a["guardado"]).unlink(missing_ok=True)
        c.execute("delete from produtos where id=?", (int(r.args[0]),))
    return {"ok": True}


def nome_seguro(n):
    n = os.path.basename(str(n).replace("\\", "/"))
    n = re.sub(r"[^\w.\- ()]", "_", n, flags=re.U).strip(" .")[:120]
    return n or "arquivo"


@route("POST", r"/api/admin/produtos/(\d+)/arquivos", admin=True, stream=True)
def adm_upload(r):
    """Corpo = bytes do arquivo (sem JSON). Metadados na query: nome, versao, plataforma, notas."""
    pid = int(r.args[0])
    n = int(r.h.headers.get("Content-Length") or 0)
    if n <= 0:
        raise ApiError(400, "Arquivo vazio")
    if n > MAX_UPLOAD:
        raise ApiError(413, f"Arquivo maior que o limite de {MAX_UPLOAD // 1048576} MB (ajuste MAX_UPLOAD_MB)")
    with conn() as c:
        if not c.execute("select 1 from produtos where id=?", (pid,)).fetchone():
            raise ApiError(404, "Programa não encontrado")
    guardado = secrets.token_hex(16)
    destino = ARQUIVOS / guardado
    sha = hashlib.sha256()
    restante = n
    with open(destino, "wb") as f:
        while restante > 0:
            parte = r.h.rfile.read(min(1 << 20, restante))
            if not parte:
                f.close()
                destino.unlink(missing_ok=True)
                raise ApiError(400, "Envio interrompido")
            f.write(parte)
            sha.update(parte)
            restante -= len(parte)
    with conn() as c:
        cur = c.execute("insert into arquivos(produto_id,nome,guardado,versao,plataforma,notas,tamanho,sha256,criado) values(?,?,?,?,?,?,?,?,?)",
                        (pid, nome_seguro(r.q.get("nome")), guardado, str(r.q.get("versao") or "")[:20],
                         str(r.q.get("plataforma") or "Windows")[:20], str(r.q.get("notas") or "")[:300], n, sha.hexdigest(), agora()))
    return {"id": cur.lastrowid, "sha256": sha.hexdigest()}


@route("DELETE", r"/api/admin/arquivos/(\d+)", admin=True)
def adm_arq_apaga(r):
    with conn() as c:
        a = c.execute("select * from arquivos where id=?", (int(r.args[0]),)).fetchone()
        if a:
            c.execute("update arquivos set ativo=0 where id=?", (a["id"],))
            (ARQUIVOS / a["guardado"]).unlink(missing_ok=True)
    return {"ok": True}


def valida_ate(v):
    v = str(v or "").strip()
    if v:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ApiError(400, "Data de validade inválida")
    return v


@route("POST", "/api/admin/clientes", admin=True)
def adm_cli_novo(r):
    b = r.body
    email = email_ok(b.get("email"))
    senha = str(b.get("senha") or "").strip() or senha_aleatoria()
    if len(senha) < 8:
        raise ApiError(400, "A senha precisa ter pelo menos 8 caracteres")
    sal, h = hash_senha(senha)
    with conn() as c:
        if c.execute("select 1 from clientes where email=?", (email,)).fetchone():
            raise ApiError(409, "Já existe um cliente com este e-mail")
        cur = c.execute("insert into clientes(email,nome,whatsapp,sal,hash,criado) values(?,?,?,?,?,?)",
                        (email, str(b.get("nome") or "").strip()[:80], re.sub(r"\D", "", str(b.get("whatsapp") or ""))[:20], sal, h, agora()))
        cid = cur.lastrowid
        if b.get("produto_id"):
            c.execute("insert into acessos(cliente_id,produto_id,chave,ate,criado) values(?,?,?,?,?)",
                      (cid, int(b["produto_id"]), str(b.get("chave") or "").strip()[:2000], valida_ate(b.get("ate")), agora()))
    return {"id": cid, "email": email, "senha": senha}


@route("PUT", r"/api/admin/clientes/(\d+)", admin=True)
def adm_cli_edita(r):
    b = r.body
    with conn() as c:
        cid = int(r.args[0])
        if "senha" in b and str(b["senha"]).strip():
            if len(str(b["senha"])) < 8:
                raise ApiError(400, "A senha precisa ter pelo menos 8 caracteres")
            sal, h = hash_senha(str(b["senha"]))
            c.execute("update clientes set sal=?, hash=? where id=?", (sal, h, cid))
            c.execute("delete from sessoes where cliente_id=?", (cid,))
        if "nome" in b:
            c.execute("update clientes set nome=? where id=?", (str(b["nome"]).strip()[:80], cid))
        if "ativo" in b:
            c.execute("update clientes set ativo=? where id=?", (int(bool(b["ativo"])), cid))
            if not b["ativo"]:
                c.execute("delete from sessoes where cliente_id=?", (cid,))
    return {"ok": True}


@route("POST", r"/api/admin/clientes/(\d+)/nova-senha", admin=True)
def adm_cli_senha(r):
    senha = senha_aleatoria()
    sal, h = hash_senha(senha)
    with conn() as c:
        c.execute("update clientes set sal=?, hash=? where id=?", (sal, h, int(r.args[0])))
        c.execute("delete from sessoes where cliente_id=?", (int(r.args[0]),))
    return {"senha": senha}


@route("DELETE", r"/api/admin/clientes/(\d+)", admin=True)
def adm_cli_apaga(r):
    with conn() as c:
        c.execute("delete from clientes where id=?", (int(r.args[0]),))
    return {"ok": True}


@route("POST", "/api/admin/acessos", admin=True)
def adm_acesso(r):
    """Libera (ou atualiza) um programa para um cliente: chave de licenca e validade opcionais."""
    b = r.body
    cid, pid = int(b.get("cliente_id") or 0), int(b.get("produto_id") or 0)
    with conn() as c:
        if not c.execute("select 1 from clientes where id=?", (cid,)).fetchone() or not c.execute("select 1 from produtos where id=?", (pid,)).fetchone():
            raise ApiError(404, "Cliente ou programa não encontrado")
        c.execute("insert into acessos(cliente_id,produto_id,chave,obs,ate,ativo,criado) values(?,?,?,?,?,1,?) "
                  "on conflict(cliente_id,produto_id) do update set chave=excluded.chave, obs=excluded.obs, ate=excluded.ate, ativo=excluded.ativo",
                  (cid, pid, str(b.get("chave") or "").strip()[:2000], str(b.get("obs") or "").strip()[:200], valida_ate(b.get("ate")), agora()))
    return {"ok": True}


@route("PUT", r"/api/admin/acessos/(\d+)", admin=True)
def adm_acesso_ativo(r):
    with conn() as c:
        c.execute("update acessos set ativo=? where id=?", (int(bool(r.body.get("ativo"))), int(r.args[0])))
    return {"ok": True}


@route("DELETE", r"/api/admin/acessos/(\d+)", admin=True)
def adm_acesso_apaga(r):
    with conn() as c:
        c.execute("delete from acessos where id=?", (int(r.args[0]),))
    return {"ok": True}


@route("PUT", "/api/admin/config", admin=True)
def adm_config(r):
    with conn() as c:
        for k in CFG_PADRAO:
            if k in r.body:
                c.execute("insert into config(chave,valor) values(?,?) on conflict(chave) do update set valor=excluded.valor",
                          (k, str(r.body[k]).strip()[:500]))
    return {"ok": True}


# ---------- servidor ----------
class H(BaseHTTPRequestHandler):
    server_version = "Loja/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def admin_ok(self):
        if not ADMIN_SENHA:
            return False
        a = self.headers.get("Authorization", "")
        if a.startswith("Basic "):
            try:
                _, _, senha = base64.b64decode(a[6:]).decode("utf-8", "replace").partition(":")
            except ValueError:
                return False
            return hmac.compare_digest(senha.encode(), ADMIN_SENHA.encode())
        return False

    def _send(self, status, corpo, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def _json(self, status, obj, extra=None):
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8", extra)

    def _arquivo(self, s):
        tamanho = s.caminho.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", s.ctype)
        self.send_header("Content-Length", str(tamanho))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(s.nome))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(s.caminho, "rb") as f:
            while True:
                parte = f.read(1 << 20)
                if not parte:
                    break
                self.wfile.write(parte)

    def _handle(self, metodo):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            for m, rx, fn, adm, stream in ROTAS:
                mt = rx.match(u.path)
                if m != metodo or not mt:
                    continue
                if adm:
                    if not ADMIN_SENHA:
                        raise ApiError(403, "Painel desativado: defina a variável ADMIN_SENHA no servidor.")
                    if not self.admin_ok():
                        return self._send(401, b'{"erro":"Senha do administrador necessaria"}', "application/json",
                                          {"WWW-Authenticate": 'Basic realm="Admin"'})
                body = {}
                if metodo in ("POST", "PUT", "DELETE") and not stream:
                    if "application/json" not in (self.headers.get("Content-Type") or ""):
                        raise ApiError(415, "Requisição inválida")
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 1_000_000:
                        raise ApiError(413, "Requisição grande demais")
                    raw = self.rfile.read(n) if n else b""
                    try:
                        body = json.loads(raw or b"{}")
                    except ValueError:
                        raise ApiError(400, "JSON inválido")
                    if not isinstance(body, dict):
                        raise ApiError(400, "JSON inválido")
                elif stream and "application/octet-stream" not in (self.headers.get("Content-Type") or ""):
                    raise ApiError(415, "Requisição inválida")
                res = fn(Req(self, mt.groups(), body, q))
                if isinstance(res, Stream):
                    return self._arquivo(res)
                if isinstance(res, Raw):
                    return self._send(res.status, res.corpo, res.ctype, res.headers)
                return self._json(200, res)
            raise ApiError(404, "Não encontrado")
        except ApiError as e:
            self._json(e.status, {"erro": e.msg})
            if self.command in ("POST", "PUT") and e.status in (413, 415, 400):
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:  # noqa: BLE001
            print("ERRO:", repr(e), file=sys.stderr)
            try:
                self._json(500, {"erro": "Erro interno"})
            except Exception:  # noqa: BLE001
                pass

    do_GET = lambda self: self._handle("GET")  # noqa: E731
    do_HEAD = lambda self: self._handle("GET")  # noqa: E731
    do_POST = lambda self: self._handle("POST")  # noqa: E731
    do_PUT = lambda self: self._handle("PUT")  # noqa: E731
    do_DELETE = lambda self: self._handle("DELETE")  # noqa: E731


if __name__ == "__main__":
    init_db()
    print(f"\n  Loja rodando!\n  Vitrine:  http://localhost:{PORT}/\n  Admin:    http://localhost:{PORT}/admin")
    if not ADMIN_SENHA:
        print("  ATENCAO: ADMIN_SENHA nao definida -> o painel /admin fica DESATIVADO. Defina e reinicie.")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
