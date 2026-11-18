# Features Futuras e Melhorias — `homelab-mediaserver`

Este documento consolida o backlog técnico e arquitetural de evoluções planejadas para o Spoke `homelab-mediaserver` (`spoke-mediaserver`).

---

## 1. Tema Abyss + Carrossel Spotlight no App da TV Samsung (Tizen OS / `jellyfin-tizen`)

### Contexto e Diagnóstico

O app oficial **Jellyfin for Tizen** (`jellyfin-tizen`) empacota sua própria cópia estática do `jellyfin-web` dentro do arquivo `.wgt` instalado na memória da TV (executando sob origem local `file://` com `Origin: null`) e consome o servidor Jellyfin apenas via API REST.

Nos testes e inspeções realizados, identificamos por que o tema Abyss e o Spotlight não carregam nativamente na TV hoje:

1. **`@import` relativo no `branding.xml`:**
   - O servidor entrega `<CustomCss>@import url('/web/ui/abyss.css?v=2f809171-r6');</CustomCss>` via `GET /Branding/Configuration`.
   - O componente `CustomCss` do `jellyfin-web` injeta essa string diretamente em uma tag `<style>` sem reescrever URLs relativas.
   - Em uma página local (`file://`) na TV, `/web/ui/abyss.css` resolve para o sistema de arquivos interno da TV (`file:///web/ui/abyss.css`), falhando em carregar as cores do Abyss e as fontes (`Google Sans` e `Material Icons Round`).
2. **Roteamento da TV na LAN vs. Tailscale:**
   - O domínio `https://jellyfin.greis.dev.br` resolve no DNS para o IP Tailscale do Hub (`100.109.244.123`).
   - Como a TV Samsung não executa cliente Tailscale, ela acessa o servidor pela LAN em `http://192.168.1.20:8096` (redirecionado via DNAT no Proxmox para `172.28.64.100:8096`).
   - Um `@import` fixo apontando para `https://jellyfin.greis.dev.br/...` falharia na TV sem Tailscale, enquanto um `@import` apontando para `http://192.168.1.20:8096/...` seria bloqueado por **Mixed Content** nos navegadores que acessam via HTTPS.
3. **Execução do Spotlight sob `file://` (`Origin: null`):**
   - O `<CustomCss>` do servidor injeta apenas `<style>`, portanto não executa os scripts `ui/abyss-defaults.js` nem `ui/spotlight-loader.js` que no servidor são injetados no `/jellyfin/jellyfin-web/index.html`.
   - Mesmo copiando os arquivos de `runtime/` para dentro de um pacote `.wgt` (`file://`), três pontos do código atual do Spotlight impedem o funcionamento em origem `file:` / `"null"`:
     - **`getBaseUrl()` (`runtime/spotlight.html`):** Só recorre ao `localStorage.getItem('jellyfin_credentials')` dentro do bloco `catch` de `window.top.location`. Em `file://`, o acesso a `window.top.location` não lança exceção, mas retorna `protocol: "file:"` e `host: ""`, resultando em URL base vazia.
     - **`normalizeAddress(value)` (`runtime/spotlight.html`):** Chama `new URL(value, window.location.origin)`. Como `window.location.origin` em `file://` é a string `"null"`, o Chromium lança `TypeError: Invalid base URL` mesmo quando `value` já é uma URL absoluta (`http://192.168.1.20:8096`).
     - **`postMessage` e controle de foco (`runtime/spotlight-loader.js` e `runtime/spotlight.html`):** A trava `if (!targetOrigin || targetOrigin === "null") return;` em `postToFrame` descarta as mensagens `tv-focus`, `pause` e `resume` quando a origem é `"null"`.

---

### Plano de Implementação (Em 2 Etapas)

#### Etapa A — Server-Side (`docker/jellyfin-abyss` e `kubernetes/apps/jellyfin.yaml`)

1. **CSS Embutido (Self-Contained) no `branding.xml`:**
   - Gerar variante do `abyss.css` com as 5 fontes `.woff2` (`Google Sans` Latin/Latin-Ext normal/itálico + `Material Icons Round`) embutidas em `data:font/woff2;base64,...` (~530 KiB brutos, ~260 KiB comprimidos via gzip).
   - Atualizar o `initContainer` `configure-jellyfin` em [kubernetes/apps/jellyfin.yaml](../kubernetes/apps/jellyfin.yaml) para substituir de forma **idempotente** o bloco delimitado entre `/* HOMELAB ABYSS START */` e `/* HOMELAB ABYSS END */` mesmo quando `/config/config/branding.xml` já existir no PVC `jellyfin-config` (preservando `<LoginDisclaimer>` e `<SplashscreenEnabled>`).
   - **Benefício:** Como o CSS viaja dentro do próprio JSON de `GET /Branding/Configuration` pela mesma conexão que o cliente já abriu com o servidor, funciona simultaneamente na TV via LAN (`192.168.1.20:8096`) e nos navegadores via HTTPS/Tailscale (`jellyfin.greis.dev.br`), sem depender de DNS ou `@import` externo.

2. **Suporte a `file://` no `prepare.py` (`spotlight.html` e `spotlight-loader.js`):**
   - **`normalizeAddress(value)`:** Passar `window.location.origin` como base de `new URL()` apenas quando a origem iniciar por `http://` ou `https://`; caso contrário, instanciar `new URL(value)` diretamente exigindo protocolo `http:` ou `https:`.
   - **`getBaseUrl()`:** Se `window.top.location.protocol` não for `http:` nem `https:`, resolver o endereço diretamente a partir de `jellyfin_credentials` (`server.ManualAddress || server.LocalAddress || server.RemoteAddress`).
   - **`postMessage` entre parent e iframe em `file://`:** Quando `window.location.protocol === "file:"` (`origin === "null"`), utilizar `"*"` como `targetOrigin` na chamada `postMessage`, mantendo na recepção a checagem estrita de `event.source === currentIframe.contentWindow` (no loader), `event.source === window.parent` (no iframe) e `event.origin === window.location.origin`.
   - Adicionar testes unitários em `docker/jellyfin-abyss/test-runtime.mjs` cobrindo o contexto `file://` (`origin === "null"`).

#### Etapa B — Client-Side na TV Samsung (Pacote `.wgt` Customizado via Sideload)

1. **Empacotamento do `Jellyfin.wgt` com Abyss + Spotlight:**
   - No build do `jellyfin-tizen`, copiar os artefatos verificados de `docker/jellyfin-abyss/runtime/` (`abyss-defaults.js`, `spotlight-loader.js`, `spotlight.html`, `spotlight.css`, `abyss.css` e fontes `.woff2`) para `www/ui/`.
   - Injetar no `www/index.html` do pacote `.wgt`:
     - `<script src="ui/abyss-defaults.js" data-abyss-defaults></script>` no início do `<head>` (garantindo que `<userId>-enableBackdrops` seja inicializado como `"true"` automaticamente no `localStorage` da TV);
     - `<script src="ui/spotlight-loader.js" data-abyss-spotlight></script>` antes de `</body>` (ativando o carrossel Spotlight na Home da TV com navegação via controle remoto `.tv-mode` e suporte ao keycode `10009` do botão *Return* do Tizen).
2. **Instalação via Sideload (`sdb`):**
   - Com o *Developer Mode* ativo na TV Samsung na mesma LAN:
     ```bash
     sdb connect <IP_DA_TV>:26101
     sdb install Jellyfin-Abyss.wgt
     ```

---

### Limites Conhecidos

- **Preferências locais (`userSettings.js`):** `enableBackdrops` e `disableCustomCss` são preferências locais por dispositivo (`localStorage`), não sincronizadas via `DisplayPreferences` do servidor. Com o `abyss-defaults.js` dentro do `.wgt` (Etapa B), `enableBackdrops` passa a ser ativado automaticamente na TV; apenas com a Etapa A, deve ser ativado uma vez em **Perfil → Exibição** no app da TV.
- **Versão do Chromium no Tizen OS (`:has()`):** O seletor `:has()` usado na regra `.layout-tv #itemDetailPage:has(.btnReplay:not(.hide)) .btnPlay .detailButton-content::after { content: "Resume"; }` ([runtime/abyss.css](../docker/jellyfin-abyss/runtime/abyss.css)) requer Chromium ≥ 105 (Tizen 8 / TVs 2024+). Em TVs com Tizen 7 (Chromium 94) ou anterior, a regra base sem `:has()` permanece ativa e o botão exibe `"Play"` normalmente.

---

## 2. Automação de Atualizações de Imagens (Renovate Bot + CI do `jellyfin-abyss`)

### Objetivo
Evitar que imagens fixadas por tag/digest (`@sha256:...`) fiquem defasadas ao longo do tempo, mantendo o determinismo do GitOps e a verificação `fail-closed` do `index.html` do Jellyfin.

### Escopo Planejado
1. **Renovate Bot (`renovate.json`):**
   - Monitorar automaticamente novas releases das imagens em `kubernetes/apps/*.yaml` (`radarr`, `sonarr`, `prowlarr`, `bazarr`, `byparr`, `qbittorrent`, `seerr`, `recyclarr`, `arr-mcp`, `intel-gpu-plugin`) e do `FROM jellyfin/jellyfin@sha256:...` em [docker/jellyfin-abyss/Dockerfile](../docker/jellyfin-abyss/Dockerfile).
   - Abrir Pull Requests automáticos no GitHub com o changelog da release. Após o merge na `main`, o FluxCD aplica a atualização no K3s em até 5 minutos.
2. **GitHub Actions para Build & Push do `homeserver-jellyfin-abyss` no GHCR:**
   - Automatizar `python3 docker/jellyfin-abyss/prepare.py --check`, `node test-defaults.mjs`, `node test-runtime.mjs` e o `docker build` da imagem `ghcr.io/reisguilherme/homeserver-jellyfin-abyss`.
   - Caso uma nova versão do Jellyfin altere o `SHA256` do `index.html`, o CI falha no Pull Request antes de qualquer deploy no cluster, permitindo revisar e atualizar o hash com segurança.
