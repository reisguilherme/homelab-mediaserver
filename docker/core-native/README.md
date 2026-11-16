# Runtime Python derivado para os aplicativos nativos

Recipe mínima para as quatro imagens recuperadas que não tinham Python nos
caminhos auditados. Ela instala o pacote nativo da mesma distribuição, copia o
guard, desativa o healthcheck herdado e deixa `CMD` vazio. Iniciar a imagem sem
argumentos termina em recusa 78; o build não inicia aplicativos.

Versões observadas pela operação nas imagens x86_64 em 16/11/2026:

| Papel | Alpine | APK | `PYTHON_APK_VERSION` |
| --- | --- | --- | --- |
| Sonarr | 3.22.2 | 2.14.9 | `3.12.16-r0` |
| Radarr | 3.24.2 | 3.0.8-r0 | `3.14.10-r1` |
| Prowlarr | 3.21.3 | 2.14.6 | `3.12.16-r0` |
| Seerr | 3.23.5 | 3.0.6-r0 | `3.12.16-r0` |

São pins observados, não defaults ou autorização para trocar a imagem base.
`BASE_IMAGE` e `PYTHON_APK_VERSION` são obrigatórios. Não há fallback para tag
pública nem cópia de interpretador isolado. O APK mantém seus repositórios e
chaves padrão, verificando as assinaturas; não usa `--allow-untrusted` nem muda
certificados. As dependências efetivamente resolvidas ficam registradas em
`/usr/local/share/core-native/apk-installed.txt` (root 0444). Não se afirma
reprodutibilidade bit a bit: preservar essa lista e o ID final da imagem.

Antes do build, a operação deve verificar que `BASE_IMAGE` resolve **localmente**
ao ID autenticado da captura, arquitetura x86_64 e nenhum `Config.OnBuild`.
`FROM` pode executar ONBUILD herdado antes das instruções desta recipe; uma base
com esse campo preenchido exige investigação. Usar o builder que tenha acesso
ao image store local e `--pull=false`. Se ele não conseguir resolver a identidade
local pinada, parar; não substituir por uma tag remota. A seleção e resolução
efetiva da base são evidência da operação, não algo que o Dockerfile pode consultar.

O contexto deve conter somente esta recipe e o arquivo público
`scripts/core-storage-guard.py`, nos mesmos caminhos relativos. Preparar esse
contexto mínimo no SSD privado. Não enviar o repositório inteiro, `.env`, estado,
backups, mídia, inventário ou credenciais ao builder. Exemplo de argumentos, com
valores privados preenchidos pela operação e sem dados reais neste documento:

```sh
docker build --pull=false --no-cache \
  --file docker/core-native/Dockerfile \
  --build-arg BASE_IMAGE=BASE_LOCAL_PINADA \
  --build-arg PYTHON_APK_VERSION=VERSAO_APK_OBSERVADA \
  --iidfile /PRIVATE/NOVO_ARQUIVO_IMAGE_ID .
```

Manter logs de build privados e limitados. Imagens capturadas podem conter
credenciais no `Config.Env`; não usar dump de environment, inspect completo ou
shell trace. O RUN limpa o ambiente antes de APK/Python. O ENTRYPOINT também
usa `env -i` e Python `-I -B`; o guard cria o filho com sua lista técnica fixa.
Isso limpa o ambiente dos processos, **não apaga os valores herdados dos
metadados da imagem**. A imagem derivada continua privada e não deve ser publicada.

No recibo operacional privado do build, registrar os IDs base/final, arquitetura,
versão APK pedida e Python observado, hash do Dockerfile e do guard e a lista real
de pacotes instalada. Não inventar pins de dependências nem preencher os hashes
de ativação antes de produzir as respectivas evidências.

Depois do build, conferir a identidade/configuração selecionada, que a execução
padrão recusa e que o interpretador executa o guard. Os testes de compatibilidade
e de grupo de processos nas imagens reais ainda são necessários, especialmente
para o Python 3.14 do Radarr. Build bem-sucedido não prova aplicação restaurada,
contenção de tarefas, lease real ou encerramento nativo sob o supervisor. A
[ativação explícita](../../docs/runbooks/core-native-activation.md) permanece
separada da preparação desta imagem; Compose e entrypoint preparados não mudam.

Referência de semântica do builder: [Dockerfile](https://docs.docker.com/reference/dockerfile/).
