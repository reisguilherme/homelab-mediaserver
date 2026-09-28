# Instalação em Ubuntu e acesso Tailscale

O alvo é Ubuntu Server 24.04 LTS amd64 com Docker Engine e Compose v2.
Python, uv e os serviços próprios são instalados nas imagens; o operador não
precisa instalar um ambiente Python no host para executar o servidor.

## Preparar Ubuntu e Docker

Em uma máquina nova, instale ferramentas básicas e SSH:

```bash
sudo apt update
sudo apt install -y git curl ca-certificates openssl nano openssh-server
```

A [documentação Ubuntu OpenSSH](https://ubuntu.com/server/docs/how-to/security/openssh-server/)
explica o acesso remoto. Instale Docker Engine e o plugin Compose pelo
[repositório oficial para Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
Se Docker já funciona na máquina, confira a instalação existente:

```bash
docker --version
docker compose version
sudo docker run --rm hello-world
```

Para usar Docker sem `sudo`, siga
[as instruções oficiais de pós-instalação](https://docs.docker.com/engine/install/linux-postinstall/):
adicione seu usuário ao grupo `docker` e inicie uma nova sessão. Esse grupo dá
acesso administrativo ao host.

## Preparar os diretórios

A stack usa caminhos fixos:

| Host | Conteúdo |
|---|---|
| `/srv/appdata` | Bancos e configurações persistentes |
| `/srv/data` | Torrents e biblioteca no mesmo filesystem |
| `/srv/transcode` | Arquivos temporários do Jellyfin |
| `/srv/appdata/control` | É montado como `/run/homeserver` nos containers; estado e snapshots |

Escolha e monte seu armazenamento antes de começar. Se houver um disco separado,
confirme que ele está montado em `/srv/data`; criar um diretório não monta o disco.
O projeto não particiona, formata ou modifica `fstab`.

```bash
sudo mkdir -p /srv/appdata/{jellyfin,seerr,sonarr,radarr,prowlarr,bazarr,qbittorrent,control}
sudo mkdir -p /srv/data/media/{movies,tv} /srv/data/torrents /srv/transcode
sudo chown 1000:1000 /srv/appdata /srv/appdata/{jellyfin,seerr,sonarr,radarr,prowlarr,bazarr,qbittorrent,control}
sudo chown 1000:1000 /srv/data /srv/data/media /srv/data/media/{movies,tv} /srv/data/torrents /srv/transcode
df -hT /srv/data
```

Prepare essas pastas antes do Compose, pois os bind mounts são validados antes
de o init executar. Os serviços de mídia usam UID/GID `1000:1000`.
A inicialização prepara os
subdiretórios necessários; em dados preexistentes, confirme permissões antes
de iniciar. Downloads e biblioteca precisam compartilhar filesystem para que
hardlinks não criem uma segunda cópia física.

## Criar o .env

```bash
git clone https://github.com/reisguilherme/homelab-mediaserver.git
cd homelab-mediaserver
cp .env.example .env
chmod 600 .env
```

Em uma instalação nova, gere tokens sem imprimi-los:

```bash
for key in ARR_TOKEN ADMIN_TOKEN CSRF_TOKEN SONARR_API_KEY RADARR_API_KEY PROWLARR_API_KEY BAZARR_API_KEY; do
  sed -i "s/^HOMESERVER_${key}=.*/HOMESERVER_${key}=$(openssl rand -hex 16)/" .env
done
nano .env
sudo chown 1000:1000 .env
```

Preencha `HOMESERVER_ADMIN_PASSWORD` e `HOMESERVER_QBIT_PASSWORD`, ajuste as
preferências e informe credenciais de provedores que quiser habilitar.
Jellyfin e Seerr podem começar com suas API keys vazias: o operator as adota
após o bootstrap autenticado. Não execute o bloco de geração sobre uma
instalação existente: importe suas chaves atuais.

Os consumidores próprios leem esse arquivo como UID 1000; mantenha seu owner
alinhado e a permissão 0600. Se seu usuário do host tiver outro UID, use
`sudoedit .env` nas alterações posteriores.

O arquivo contém dados, não comandos shell. Não use `source`. Consulte
[configuração](configuration.md) para a sintaxe, os parâmetros e os idiomas.

## Iniciar e conectar os serviços

```bash
docker compose up -d --build
docker compose ps
docker compose run --rm operator config validate --env-file /project/.env
docker compose run --rm operator config apply --env-file /project/.env --in-container
docker compose restart control-api control-worker download-gateway telemetry host-metrics
```

Espere as APIs iniciarem antes do apply. Se um serviço ainda estiver indisponível,
consulte `docker compose logs` e repita o apply. A inicialização cria apenas
configurações ausentes; não substitui bancos existentes. O operator preserva
IDs e confirma as preferências pela API. O checkout fica montado em `/project`
nesse container para que API keys adotadas sejam salvas no próprio `.env`.

Abra o painel em `http://IP_DO_SERVIDOR:8081` e confira Jellyfin/Seerr,
bibliotecas, Arr e provedores. Indexadores começam vazios: configure-os conforme
[o guia dos serviços](runbooks/service-setup.md). Ter todos os containers
iniciados não comprova que uma fonte tenha peers ou que uma legenda esteja disponível.

## Acesso pelo Tailscale

Instale e autentique Tailscale no host seguindo
[a documentação oficial Linux](https://tailscale.com/docs/install/linux):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale status
tailscale ip -4
```

Entre na mesma tailnet no dispositivo cliente. Use o IP retornado por
`tailscale ip -4` com as portas do [README](../README.md#acessar), por exemplo
`http://IP_TAILSCALE:8096` para Jellyfin e `http://IP_TAILSCALE:8081` para o
painel. Não é necessário encaminhar portas no roteador.

O monitor qBit é um proxy somente leitura em `127.0.0.1:18080`. Para acessá-lo
na tailnet:

```bash
sudo tailscale serve --bg --http=18080 http://127.0.0.1:18080
tailscale serve status
```

Abra o endereço MagicDNS HTTP mostrado pelo comando, na porta `18080`.
[`--bg` e `--http`](https://tailscale.com/docs/reference/tailscale-cli/serve)
mantêm a publicação após reinício e servem a porta escolhida dentro da tailnet.
O login é o nativo qBit; o proxy recusa mudanças na fila e nas preferências.

## Reutilizar uma instalação

Preserve `/srv/appdata` e `/srv/data`, importe credenciais existentes no `.env`
e confira os paths esperados pelos serviços. Pare a stack anterior antes de
iniciar outra que use os mesmos bancos ou portas. Contas existentes não têm
senha redefinida pelo apply. Examine `config plan` antes de reconciliar preferências:

```bash
docker compose run --rm operator config plan --env-file /project/.env --in-container
```

Dados de um layout diferente precisam de adaptação explícita; não copie bancos
ou mídia para o repositório. Intel é opcional e exige
[verificação do hardware](runbooks/jellyfin-hardware.md).
