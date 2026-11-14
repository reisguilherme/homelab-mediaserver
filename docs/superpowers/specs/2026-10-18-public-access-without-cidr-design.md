# Acesso público sem lista de IPs — escopo proposto

Estado: **proposta para revisão; execução não autorizada nesta etapa**.
Data: 18/10/2026, America/Sao_Paulo.

O proprietário pediu fechar o escopo antes de implementar. Nesta etapa não há mudanças de senha, contas, sessões, firewall, publicação ou serviços. Este documento atualiza as premissas do [desenho de 15/10](2026-10-15-public-access-security-design.md) e do [plano anterior](../plans/2026-10-15-public-access-security.md); não constitui execução daquele plano.

## Objetivo e decisões

Permitir que familiares usem Jellyfin e Seerr por HTTPS mesmo quando seus IPs públicos mudarem, mantendo autenticação individual e reduzindo a possibilidade de uma falha nesses aplicativos atingir a rede e os componentes administrativos.

Decisões expressas do proprietário:

- Manter aplicativos nativos de TV e celular e reforçar o login do próprio Jellyfin.
- Pretensão de retirar a lista fixa de IPs dos dois serviços públicos; preservar Tailscale.
- Trocar as senhas dos usuários por senhas fortes aleatórias, geradas por código, e entregar um TXT com usuário e senha sem exibir seu conteúdo ao agente.
- Apenas definir o escopo agora.

Recomendações deste documento, ainda sujeitas à revisão do proprietário:

- Manter os dois domínios e a porta pública existentes; nenhuma mudança automática no roteador.
- Não exigir uma tela extra de senha, CAPTCHA ou autenticação interativa de proxy no caminho dos aplicativos Jellyfin. Não criar bypass por User-Agent para tentar contornar incompatibilidades.
- Usar login individual e privilégios mínimos; administração somente pelo caminho privado.
- Concluir correções de aplicação e contenção antes de retirar o CIDR. Segmentação externa aos hosts constitui uma entrega adicional, explicitamente distinta.

## 1. Acesso discreto e compatível

Somente Jellyfin e Seerr ficam acessíveis pela entrada HTTPS pública. qBit, *arr, status, controlador, SSH e painéis administrativos permanecem privados. Hosts desconhecidos e acessos por IP sem o hostname esperado não encaminham para os aplicativos; isso reduz exposição casual, mas não autentica clientes.

Ocultar a lista pública de usuários, desabilitar cadastro/criação automática de contas não aprovadas e exigir login para catálogo, reprodução e pedidos. Manter somente os endpoints anônimos estritamente necessários a login, descoberta do servidor e assets dos clientes. O conjunto será validado com aplicativos reais; não prometer ocultação de toda informação de versão/servidor quando o protocolo exigir informações públicas.

Adicionar instruções de não indexação e evitar links públicos de divulgação. `robots.txt`, `noindex`, hostname difícil e porta alternativa não são controles de autorização. Certificados públicos podem tornar os nomes descobertos por registros de transparência; links também podem ser repassados. A meta é acesso com conta autorizada, mesmo que alguém descubra o endereço.

Uma página que apenas pede senha e redireciona pode ser ignorada acessando o destino diretamente. Uma barreira real precisa proteger também API, WebSocket, imagens e streaming e pode ser incompatível com aplicativos. Ela fica fora do escopo escolhido. Autenticação adicional com MFA no Seerr, que é usado pelo navegador, poderá ser uma evolução separada, sem tornar a presente entrega dependente de novo provedor de identidade.

## 2. Contas, senhas e sessões

Abranger todas as contas humanas Jellyfin e as contas locais Seerr, incluindo administradores; contas Seerr vinculadas ao Jellyfin devem continuar usando a identidade vinculada, sem duplicação artificial de senha. Preservar IDs, histórico, favoritos, permissões legítimas e pedidos. Inventariar separadamente contas e chaves de serviço para coordenar qualquer rotação necessária com as integrações.

Proposta técnica: uma senha distinta por identidade, com 24 caracteres aleatórios de um alfabeto compatível com os clientes, gerada com CSPRNG (`secrets`), sem nome, data ou senha compartilhada. Uma conta administrativa separada terá uso privado; a conta cotidiana do proprietário permanece apta à reprodução pública com permissões comuns.

Na futura execução, um programa local deve gerar, aplicar e verificar as credenciais sem imprimir senha, corpo de requisição/resposta, cookies ou tokens. Gerar primeiro um arquivo privado com ACL restrita ao proprietário e aos administradores necessários do sistema, sem herança de leitura para outros usuários. Gravar diretamente nesse arquivo e atualizar um estado sanitizado de sucesso/falha por conta. Falha parcial não pode perder uma senha já aplicada nem induzir uma segunda rotação cega.

Destino padrão proposto: arquivo local em área privada `.runtime`, ignorado pelo Git, fora de qualquer pasta servida por HTTP. O usuário receberá apenas o caminho do TXT e os resultados de aplicação. O agente não abrirá, exibirá, indexará, anexará nem copiará seu conteúdo para o chat. O programa precisará manipular as senhas em memória para aplicá-las e testar login. O TXT é deliberadamente texto simples, conforme solicitado; ACL não equivale a criptografia. Não enviá-lo automaticamente a familiares nem mantê-lo como cópia no servidor público.

Revogar as sessões/tokens de usuário anteriores à rotação e verificar que uma sessão antiga falha. Troca de senha sozinha não comprova revogação. Validar separadamente sessões Seerr e tokens Jellyfin e coordenar exceções de integração: nenhuma chave administrativa pode tornar-se uma credencial pública. Preservar um caminho de recuperação privado testado antes da rotação. Os usuários precisarão autenticar novamente seus dispositivos.

Desabilitar permissões públicas de exclusão, gerenciamento do servidor e administração dos pedidos. A negação deve abranger APIs e tokens administrativos previamente emitidos, e não apenas esconder botões. Permissões de reprodução/pedidos serão mantidas dentro do perfil de cada usuário; não alterar aprovações automáticas ou limites da fila como efeito colateral.

## 3. Tentativas de login e abuso

Aplicar limites nas rotas reais de autenticação de ambos os aplicativos, incluindo variantes de caminho, recuperação de senha e Quick Connect quando habilitados. Limites de autenticação não devem limitar segmentos de vídeo ou invalidar sessões legítimas já autenticadas.

Combinar limitação por origem real, por conta/identidade normalizada e um teto agregado de autenticação. Contagem apenas por IP não cobre ataques distribuídos; bloquear permanentemente uma conta após poucas falhas permite indisponibilizá-la deliberadamente.

Parâmetros iniciais propostos para ensaio, ajustáveis após teste com clientes: cinco falhas em dez minutos por origem geram bloqueio temporário de quinze minutos nas rotas de autenticação; reincidência aumenta a janela para uma hora e depois até vinte e quatro horas. Por conta, aplicar espera progressiva temporária; definir taxa e teto agregado no plano técnico a partir do comportamento observado. Não tratar erro 5xx do backend como senha errada. Não habilitar o bloqueio nativo permanente após cinco erros como único mecanismo.

Preferir componentes mantidos de rate limiting e proteção contra força bruta, compatíveis com a stack existente. O plano técnico escolherá o mecanismo que comprovadamente suporte a política sem criar um novo sistema próprio de autenticação ou units próprias no host. Se uma capacidade não existir, registrar o limite antes de implantação, sem fingir que um simples filtro de IP oferece proteção por conta.

O IP vem da conexão na borda e de uma cadeia fechada de proxies confiáveis. Descartar cabeçalhos de origem fornecidos pelo cliente e aceitar os reescritos somente dos proxies previstos. Testar `Forwarded`, `X-Forwarded-For`, `X-Real-IP`, IPv6 e acessos alternativos. Evitar bloquear a TV box ou todo o HomeServer por registrar o proxy como origem. Recuperação e inspeção de bloqueios permanecem privadas; não liberar toda a tailnet ou LAN como administradores por conveniência.

## 4. Correções e contenção obrigatórias

Revalidar versões instaladas, avisos de segurança, plugins e compatibilidade antes de escolher versões corrigidas de Jellyfin, Seerr e proxy. Os números de versão citados no plano anterior são candidatos históricos, não uma instrução para instalar sem revisão. Ensaiar migração em cópias privadas e preparar retorno coerente de imagem/configuração/banco, sem criar gerenciador de backups.

Separar o relay de apresentação/mídia que atualmente compartilha o controlador privilegiado. Componentes alcançáveis publicamente não recebem `.env` inteiro, banco do controlador, chaves administrativas globais ou Docker socket. Reduzir ambientes, capacidades, montagens e destinos de rede por função.

A borda só alcança os dois upstreams necessários. Jellyfin e Seerr não podem iniciar conexões livres ao roteador, SSH, qBit, controlador, outras máquinas ou tailnet. Integrações necessárias com *arr passam por caminhos delimitados; validar métodos, rotas, parâmetros e destinos fixos. Impedir uso de funcionalidades de proxy/URL para atingir redes internas, inclusive por redirecionamento e resolução DNS para IP privado.

Aplicar e testar políticas IPv4 e IPv6, inclusive portas publicadas pelo Docker, gateways, endereços link-local e endereços IPv6 globais dos equipamentos locais. O acesso direto aos backends não pode contornar os controles públicos. Tailscale mantém acesso privado autorizado e recebe revisão de ACL/grants para impedir que a borda herde acesso amplo à rede.

Proteger cookies, CSRF e origem do Seerr; HTTPS privado deve estar funcional antes de exigir cookies seguros. Logs preservam diagnóstico de falhas/bloqueios sem senhas, tokens, cookies ou URLs com credenciais. Limites de recursos e conexões serão medidos para evitar que uma enxurrada de requisições derrube os serviços, preservando reprodução, seek, legendas, WebSockets e transcode.

## 5. Proteção da rede doméstica: limite da entrega

A TV box está na mesma LAN do HomeServer. Firewall por processo, containers e Tailscale restrito reduzem o alcance de uma falha, mas root em um host pode contornar controles daquele host. Uma tela de senha não resolve esse risco.

Para afirmar isolamento dos demais dispositivos mesmo após comprometimento do host, é necessária uma barreira fora dele: segmentos/VLANs com ACL aplicadas no equipamento de rede, ou topologia equivalente comprovada. A borda e o HomeServer devem ser considerados; isolar somente a TV box não contém um HomeServer comprometido. “DMZ host” de roteador não será considerado segmentação.

Nesta proposta, a primeira entrega cobre aplicações, processos e controles de host. A segunda é a investigação e o projeto de segmentação externa, com configuração de roteador pelo proprietário e eventual equipamento a decidir. A primeira entrega pode permitir acesso público após seus testes, mas mantém explicitamente o risco residual de comprometimento root e exige aprovação desse escopo pelo proprietário. Não declarar “ninguém consegue acessar sua rede” ao terminar apenas a primeira entrega.

Jellyfin mantém escrita para edições e legendas conforme as regras atuais. Uma falha com esse acesso ainda pode alterar mídia. Tornar a mídia somente leitura e delegar escrita a um processo privado é uma evolução separada, dependente de desenho e autorização de montagens; não será feita silenciosamente. Controles locais também não impedem saturação do link residencial por DDoS volumétrico.

## 6. Sequência e aceite

1. Atualizar o plano técnico antigo para substituir expressamente CIDR obrigatório e bloqueio permanente simplista pelas políticas acima; verificar o que já existe em produção.
2. Validar acesso privado de recuperação, migrações e reversão; corrigir versões, segredos, relay, permissões e rede.
3. Implementar e testar login, antiabuso e administração exclusivamente privada em ambiente isolado e depois em janela controlada.
4. Rotacionar credenciais e revogar sessões com registro sanitizado e TXT privado recuperável.
5. Testar navegador, TV e celular; login, reprodução, legendas, seek, reconexão, pedidos e acesso privado precisam funcionar.
6. Validar negações: anônimo não vê catálogo/reproduz, credenciais antigas falham, token administrativo não funciona publicamente, headers forjados não alteram origem/privilégio, backends e redes internas não são alcançáveis pelos componentes públicos fora das exceções.
7. Somente após esses critérios, retirar a lista fixa de IPs nos pontos de aplicação existentes, validar externamente de uma origem antes não permitida e confirmar persistência após recriação/reinício controlado. Manter restrições de portas, destinos, identidade e administração. Falha crítica mantém o acesso público restrito ou em manutenção, com recuperação privada funcional.

Nenhuma exclusão/movimentação de mídia, reset de aplicações, alteração da fila de downloads, formatação, mudança de montagem ou abertura automática de portas faz parte desta entrega. Os testes operacionais não equivalem a promessa de ausência de vulnerabilidades ou ensaio de resistência a DDoS.

## Fontes primárias consultadas

- [Jellyfin: usuários, ocultação e bloqueio manual](https://jellyfin.org/docs/general/server/users/).
- [Jellyfin: proteção contra força bruta e cuidados com proxies](https://jellyfin.org/docs/general/post-install/networking/advanced/fail2ban/).
- [Caddy: reverse proxy e cabeçalhos confiáveis](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).
- [OWASP: autenticação, throttling e risco de bloquear contas de terceiros](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html).
- [Let's Encrypt: transparência dos certificados](https://letsencrypt.org/docs/ct-logs/).

As páginas orientam os controles; não comprovam que estejam instalados nem autorizam copiar exemplos incompatíveis com as restrições deste projeto.
