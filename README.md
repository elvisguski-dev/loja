# 🛒 Loja de programas

Site para **vender e entregar seus programas** (PetAgenda, OrçaZap, PhotoFesta…): vitrine pública, **área do cliente com login**, **downloads protegidos** (só quem comprou baixa) e **chave de licença** de cada cliente. Só usa o Python 3.9+ padrão e SQLite.

Compra manual: o cliente chama pelo WhatsApp, paga por Pix e você libera o acesso no painel `/admin` em 1 minuto.

## Rodar

```bash
ADMIN_SENHA="uma-senha-forte" python3 server.py
```

- Vitrine: `http://localhost:8082/`
- Admin: `http://localhost:8082/admin` (usuário: qualquer; senha: `ADMIN_SENHA`). **Sem `ADMIN_SENHA` o admin fica desativado.**

Variáveis: `PORT` (8082), `LOJA_DB` (caminho do banco; os arquivos ficam em `arquivos/` ao lado), `MAX_UPLOAD_MB` (500), `TRUST_PROXY=1` (atrás de proxy/HTTPS, para registrar o IP real).

## Fluxo de venda

1. **Programas** → cadastre cada programa (nome, descrição, preço) e **envie o arquivo** (zip do instalador, com versão). Para publicar atualização, envie a nova versão: o cliente vê a mais recente primeiro.
2. **Loja** → coloque seu WhatsApp (só números, com 55 + DDD). O botão *Comprar pelo WhatsApp* já abre a conversa com o nome do programa.
3. Cliente pagou? **Clientes e acessos → Novo cliente**: e-mail, programa, validade (opcional) e a **chave de licença** (cole a que o gerador de licenças do PetAgenda criou). O sistema gera a senha e monta a mensagem pronta para você copiar e enviar.
4. O cliente entra, baixa o instalador e copia a chave. Para renovar: **Liberar programa** de novo com a nova chave/validade (se a validade passar, o download e a chave somem da conta dele, até renovar).
5. **Bloquear** derruba as sessões do cliente na hora.

## Hospedagem (precisa de servidor, não funciona em GitHub Pages)

Os downloads são protegidos por login, então o site precisa rodar em um servidor:
- **VPS** barata (Hetzner, Contabo, Oracle Free) com `systemd` + Caddy (HTTPS automático); ou
- **Docker** (`Dockerfile` incluso) em Fly.io/Railway/Render com **volume persistente** montado em `/dados` — sem volume você perde clientes e arquivos a cada deploy.

Use sempre **HTTPS** (o cookie de sessão é `HttpOnly` + `SameSite=Lax`, e `Secure` quando o proxy envia `X-Forwarded-Proto: https`). Faça backup da pasta `dados/`.

## Segurança

- Senhas com PBKDF2-SHA256 (200 mil iterações); sessões aleatórias guardadas só como hash; limite de tentativas de login.
- Arquivos guardados com nome aleatório fora de qualquer pasta pública; cada download confere login + acesso ao programa + validade.
- Requisições de alteração exigem JSON (bloqueia formulários de outros sites) e o admin usa senha própria.
- Mostramos o SHA-256 de cada arquivo para o cliente conferir a integridade.
