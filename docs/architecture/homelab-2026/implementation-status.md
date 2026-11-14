# Preparação da implementação — 14/11/2026

O proprietário autorizou implementar e publicar na branch `refactor`, incluindo
as alterações locais anteriores. **O corte aguarda aviso explícito do
proprietário.** A preparação não instala Proxmox nem transforma o servidor atual
em três VMs sem essa etapa.

## Estado observado por SSH

- Servidor Ubuntu 24.04.5 LTS, com virtualização disponível. O SSD do sistema e
  o SSD de mídia são dispositivos distintos; nenhum foi escolhido para sobrescrita.
- Quatro volumes de mídia ainda compõem o mergerfs. Os três menores estão quase
  cheios. O HD de 18 TB tinha aproximadamente 6,84 TB livres na consulta; isso
  não equivale à capacidade disponível para novas admissões.
- Há staging de uma consolidação anterior e um verificador de conteúdo ativo.
  O progresso aumentou entre consultas, mas a prova global ainda não terminou.
  Existe uma pendência de reparo, que não foi aceita como perda de conteúdo.
- Jellyfin e telemetria responderam aos checks locais. O controlador respondeu
  `503` com admissão desabilitada, e o marcador de manutenção estava presente.
  Não foi removida manutenção nem reativada aquisição durante a inspeção.
- A execução usa overrides privados além dos arquivos Compose do repositório.
  Uma implantação deve preservá-los até revisão individual. Substituir a lista
  de arquivos pela configuração padrão pode desfazer proteções existentes.

Identidades de discos, endereços, credenciais, inventários, paths de incidentes
e detalhes de mídia permanecem em evidências privadas, fora do Git. Esses checks
não comprovam reprodução real, exposição WAN, desempenho ou integridade total.

## Entrega de código preparada

| Área | Preparação | Ainda necessário |
|---|---|---|
| Storage singleton | Suporte existente para `hdd3`, consumidores e recuperação incluídos na branch | Reconciliação dos dados e estado real antes do corte |
| Observabilidade | Exporter privado, publicação sanitizada opt-in, Prometheus e Grafana com provisioning | Build, ativação, renderização, permissões e consumo medidos no servidor |
| Governança | Desenho separa leitura de métricas e controle administrativo | Proxmox real, usuários/ACL por VM, MFA e tarefas administrativas ensaiadas |
| Recuperação USB | Implementação local existente e desenho de coordenação host/core/NFS | Provar ativação local; implementar e ensaiar coordenação virtualizada |
| Energia | Nobreak registrado como aquisição pendente | Equipamento, autonomia, comunicação e desligamento coordenado |

A observabilidade inicial cobre host, rede, capacidade por pool e validade/idade
dos snapshots. Não cobre ainda todas as VMs, aplicações, energia, auditoria ou
alertas. O [guia de ativação](../../observability.md) distingue preparação e runtime.

## Checkpoint privado

Foi criado um checkpoint do código/configuração efetivamente presentes no
servidor, incluindo configuração privada e os overrides ativos. O arquivo foi
relido no HD de 18 TB e copiado para este computador Windows. Tamanho e SHA-256
coincidiram nos dois destinos; o arquivo Windows está em diretório com ACL
restrita. Nenhum desses conteúdos entra na branch.

**Esse checkpoint não inclui bancos nem constitui um estado consistente de todas
as aplicações.** A cópia final dos bancos, configurações, claims e demais dados
necessários à restauração deve ser feita sob quiescência na janela autorizada,
verificada nos dois destinos e ensaiada antes de alterar o SSD do sistema.

## Próxima etapa operacional

1. Concluir/reconciliar a verificação anterior sem executar outro publicador ou
   modificá-la enquanto mantém locks do pipeline.
2. Medir conteúdo nativo versus staging, colisões e grupos de hardlinks. Espaço
   ocupado no HD de 18 TB não demonstra que tudo já esteja publicado na biblioteca.
3. Preparar publicação por lote com prova nova e estado por conteúdo. O helper
   histórico exige corte integral e cleanup; ele não atende ao requisito atual
   de disponibilizar o HD grande e acrescentar o restante ao longo dos dias.
4. Quando o proprietário liberar a parada, capturar o conjunto consistente para
   restauração, validar comandos concretos e apresentar o SSD/montagens exatos
   antes de qualquer operação destrutiva ou alteração de montagem.
5. Conservar os discos de origem. Não executar cleanup nem usar exceções antigas
   de conteúdo degradado como autorização para perda nesta migração.

O [plano operacional](../../superpowers/plans/2026-11-14-homelab-refactor-migration.md)
registra as dependências. Nenhuma cópia nova de mídia, formatação, alteração de
mount, instalação de hipervisor ou reinicialização de serviço foi executada nesta
preparação.

## Verificação do código

- Python 3.12 em WSL; `uv sync --frozen` conferiu 27 pacotes sem alterar dependências.
- Suíte unitária, de contrato, integração e sistema: **4.362 passaram, 156 pulados**.
  Os skips dependem de root, runtimes/imagens Nginx/Caddy ou Compose no WSL.
- Após a última correção do `.env`, **98 testes de configuração e isolamento de
  ambiente passaram**. Essa rodada tem sobreposição com a suíte ampla; não somar
  os números como se fossem testes distintos.
- Ruff, compilação Python, sintaxe dos nove scripts Shell, ShellCheck e as três
  fixtures Shell de sistema passaram. `git diff --check` sem erros.
- Compose normalizado com ambiente fictício, inclusive merge do publicador e
  conflito de nome de projeto. Nenhum daemon de produção foi usado nesse check.
- Fixture Linux confirmou escrita pelo UID1000, herança do GID65534 e leitura sem
  escrita pelo UID65534. Isso não valida ainda os binds reais em containers.

A revisão independente cobriu as mudanças de observabilidade e a exclusão de
artefatos privados na publicação. As alterações anteriores também estão incluídas
por autorização do proprietário; não se declara auditoria funcional integral de
todo esse histórico, scan de CVEs, teste de carga ou aceite físico.
