# NEXUS Crypto Terminal

Terminal de análise de mercado Spot da Binance com foco em fluxo observado, qualidade dos dados e sinais explicáveis. A versão hospedada usa Binance Spot e não envia ordens nem pede chaves de exchange.

## Começar no Windows

Pré-requisitos: Node.js 20+ com npm, Python 3.12+ e conexão à internet. Para uma primeira execução, dê duplo clique em `iniciar-terminal.bat`. O script instala dependências, cria um ambiente Python local, inicia a API e o frontend e abre `http://localhost:3000`.

Para iniciar manualmente, em terminais separados:

```powershell
npm install
py -3 -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
backend\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8001
```

```powershell
npm run dev
```

Copie `.env.example` para `.env.local` se precisar trocar a URL do WebSocket. API: `http://127.0.0.1:8001/health`; documentação interativa: `http://127.0.0.1:8001/docs`.

## O que está implementado

- Binance Spot para 25 pares USDT: BTC, ETH, SOL, BNB, XRP, ADA, DOGE, AVAX, LINK, DOT, LTC, BCH, SUI, TRX, NEAR, APT, ARB, OP, INJ, FIL, UNI, ETC, ATOM, AAVE e WIF.
- Cotações 24h e candles OHLCV históricos, com preço, hora de evento/recebimento e latência aproximada.
- Candles ao vivo por WebSocket e gráfico de candles/volume, intervalos de 1m a 1M.
- Trades agregados reais (aggTrades), classificação do lado agressor conforme campo `m` da Binance e deduplicação por ID. Um coletor em segundo plano acompanha os 25 pares sem depender de uma tela aberta, tenta recuperar até 5.000 IDs ausentes por lacuna via REST pública da Binance e grava trades em Postgres ou SQLite. Trades brutos duram uma hora por padrão; barras compactas de fluxo por minuto ficam até 30 dias (`FLOW_RETENTION_HOURS` e `FLOW_AGG_RETENTION_DAYS`), configurados para reduzir consumo no banco gratuito. O limite de recuperação é configurável por `FLOW_GAP_RECOVERY_MAX_TRADES`. Qualidade/latência em `/api/flow-quality` e histórico em `/api/flow-history`.
- Book agregado Spot de Binance, Kraken e Coinbase para os ativos solicitados: snapshots/deltas públicos de cada feed são agrupados em faixas de 0,1 bp, com corretoras contribuintes identificadas por nível e estado/atualização por fonte. O backend limita Kraken/Coinbase a cinco ativos usados recentemente e a 150 níveis por lado para caber no Render Free. USD é tratado como aproximadamente 1 USDT; gap e níveis consolidados são indicativos, não cotações executáveis.
- Times & Trades agrega negociações públicas de Binance, Kraken e Coinbase por horário, conserva a corretora de origem e classifica a agressão segundo cada feed. O CVD soma quantidades na unidade do ativo base, não nocional em USD/USDT. O armazenamento histórico e backfill continuam exclusivos da Binance.
- CVD da amostra visível e CVD histórico por barras persistidas. As barras contêm OHLC, quantidade/notional comprador e vendedor e contagem de trades; são criadas por trigger apenas quando um aggTrade novo é inserido, o que preserva a deduplicação. A interface oferece janelas de 6h, 24h e 7d; `/api/flow-history` aceita intervalos de 1, 5, 15, 30 e 60 minutos e informa uma estimativa de cobertura.
- Zonas diárias de volatilidade GARCH(1,1) e HV252 nos gráficos intradiários, com referência no último fechamento diário concluído, faixas lognormais e controles para ocultar cada conjunto. O candle diário ainda aberto é excluído.
- Sinais determinísticos de reversão BUY/SELL em bandas GARCH/HV252, confirmados por rejeição em candle fechado, filtro de volume (1,2× a média dos 20 candles prévios), score mínimo 50 e cooldown de 10 candles. Direções, fontes, sigma mínimo e tolerância de toque são configuráveis; marcadores aparecem no gráfico e nenhuma ordem é enviada.
- Sinal determinístico por candle fechado: EMA(9/21), RSI(14), volume relativo e rompimento de estrutura. Pesos indisponíveis continuam ausentes; pontuação não é normalizada nem apresentada como probabilidade.
- Funding, mark/index e open interest de Futures destacados como dados centrais do contrato. Fear & Greed via Alternative.me, com estado indisponível em falhas.
- Alertas de preço no navegador, armazenados localmente e sem serviço push.
- Avaliação retrospectiva acessível em `/api/reversal-evaluation`: aplica os parâmetros padrão dos sinais a candles fechados recentes, entra na abertura do candle seguinte e estima resultado após horizonte configurável, descontando taxas e slippage estimados pelo usuário.
- Scanner Spot para os 25 pares USDT, ordenável por variação, volume cotado em USDT, CVD nocional e ritmo de fluxo de 15 minutos. O fluxo usa apenas aggTrades Binance persistidos; o ritmo fica indisponível até observar três blocos anteriores e parte suficiente do bloco atual.
- Alertas locais configuráveis para preço, variação de 24h, volume cotado, ritmo de fluxo e CVD nocional. Regras, estado e cooldown de 15 minutos ficam no armazenamento do navegador; as notificações só são verificadas enquanto o terminal está aberto.
- Painel de contexto com amplitude da watchlist, Fear & Greed da Alternative.me e TVL/oferta circulante de stablecoins agregados pela DefiLlama. Consultas à DefiLlama são armazenadas em cache por uma hora e cada valor expõe fonte e horário.
- Dashboard responsivo, tema escuro, fuso America/Sao_Paulo e atribuição requerida pela licença de Lightweight Charts.

## Limites desta versão

- Footprint e heatmap ainda não estão implementados. O histórico do book começa quando o coletor inicia; esta versão não persiste snapshots e mantém estado apenas em memória.
- CVD e volume de trades no painel representam a amostra observada, não todo o volume do candle nem histórico completo. Binance, Kraken e Coinbase são mercados distintos: a agregação não representa todas as exchanges e depende de disponibilidade/listagem do par em cada uma. A API pode buscar uma amostra REST recente da Binance para preencher a primeira tela; isso não equivale a cobertura contínua do coletor.
- O sinal não incorpora fluxo nem derivativos; os fatores aparecem como ausentes no motor. Não calcula entradas, stops ou alvos.
- Alertas cruzam somente o preço para cima, são por navegador e não sincronizam entre dispositivos.
- A recuperação de lacunas é limitada e sujeita à disponibilidade/limite REST da Binance; IDs omitidos e falhas são expostos em `/api/flow-quality`. As barras históricas começam na implantação desta versão; não há importação retroativa de fluxo. A cobertura estimada não consegue distinguir um minuto sem negócios de uma lacuna do coletor. A avaliação de sinais usa até 40 sinais recentes e candles da Binance; resultados sobrepostos são independentes e não incluem funding, spread, impacto ou falhas de execução.
- Sem `DATABASE_URL`, SQLite mantém os trades enquanto o arquivo existir. Configure `FLOW_DB_PATH` para escolher o caminho e `FLOW_STORAGE_DURABLE=true` somente se estiver em disco persistente. O Render gratuito não preserva esse arquivo entre reinícios/deploys.
- Para persistir sem custo recorrente, o backend aceita `DATABASE_URL` de um Postgres como o Supabase Free. Crie um projeto, copie no painel Supabase a URL **Session pooler** e cadastre-a como variável secreta `DATABASE_URL` no serviço `nexus-terminal-api` do Render; não coloque a senha no repositório. A API passa a indicar `storage=postgres` e `storageDurable=true`. O plano Supabase Free tem 500 MB e pode pausar projetos após uma semana com baixa atividade; monitore o uso exibido no painel.
- Não há usuário, login, cobrança, planos ou autorização por assinatura. O dashboard local valida experiência e dados antes da construção dessas partes.
- Alertas são específicos deste navegador e desta instalação, dependem de a página estar aberta e não sincronizam entre dispositivos. Scanner e histórico de CVD são limitados ao fluxo Binance agregado observado desde o início da coleta; barras ausentes não permitem distinguir minuto sem negócios de falha de coleta. Ritmo relativo exige três blocos anteriores não vazios.
- TVL e oferta de stablecoins são indicadores on-chain de baixa frequência, atualizados tipicamente por hora pela DefiLlama; não medem liquidez nem fluxo de ordens Spot. Preserve a atribuição ao exibir Fear & Greed (Alternative.me) e DefiLlama.
- A avaliação retrospectiva é uma estimativa simples sobre OHLCV, não paper trading nem prova de desempenho futuro. Não simula fills intrabar, funding ou execução financeira.
- REST pública está sujeita a rate limits, disponibilidade regional e alterações da exchange. A aplicação reporta erros e não fabrica dados.

## Publicação e assinaturas

### Acesso privado por e-mail e senha

O frontend oferece login sem cadastro público. Para liberar uma conta, defina `AUTH_USERS` no serviço `nexus-terminal-web` do Render como uma lista JSON, por exemplo `[ {"email":"pessoa@exemplo.com","password":"senha-forte-com-12-caracteres"} ]`. Cada senha precisa ter pelo menos 12 caracteres. O usuário e a senha são comparados no servidor; a sessão é assinada com `AUTH_SECRET` e armazenada em cookie HTTP-only por sete dias. O Blueprint gera `AUTH_SECRET` automaticamente. Para cadastrar ou remover usuários, atualize a variável `AUTH_USERS` e salve no Render. Não coloque credenciais reais no repositório. Este primeiro acesso privado usa credenciais definidas pelo administrador, sem recuperação de senha e sem gerenciamento de usuários pela interface.

Trate o app atual como versão de validação local. Antes de disponibilizar dados em serviço pago, confirme por escrito os direitos de uso e redistribuição comercial dos endpoints, streams e datasets de cada exchange; dados acessíveis publicamente não significam automaticamente licença para redistribuição. Consulte também retenção, privacidade, termos de uso e obrigações locais. O repositório não oferece aconselhamento jurídico.

### Protótipo gratuito no Render

O arquivo `render.yaml` descreve dois Web Services Docker gratuitos: `nexus-terminal-web` (Next.js) e `nexus-terminal-api` (FastAPI). No painel do Render, crie um Blueprint a partir do repositório e confira os dois serviços e as variáveis de ambiente antes de aplicar. O `NEXUS_API_ORIGIN` é preenchido pela URL do serviço API. Se escolher outros nomes de serviço, atualize as URLs `*.onrender.com` correspondentes no `render.yaml`.

O Next.js lê `NEXUS_API_ORIGIN` durante o build, então a URL da API precisa estar disponível na criação/build do Blueprint. O serviço FastAPI responde na porta `PORT` do Render. A URL WebSocket pública usa `wss://`. Os serviços gratuitos podem dormir após 15 minutos sem tráfego recebido, demorar aproximadamente um minuto para acordar e não têm armazenamento persistente; são adequados para demonstração, não para disponibilidade prometida a assinantes. O Render pode suspender serviços sem forma de pagamento se os limites incluídos de uso ou tráfego forem excedidos.

Depois do primeiro deploy, confirme no painel que os domínios públicos correspondem aos nomes definidos, que `/health` da API retorna saudável e que `CORS_ORIGINS` contém a origem real do frontend. Em caso de renomeação, corrija `CORS_ORIGINS` e `NEXT_PUBLIC_MARKET_WS_URL` e faça novo deploy.

Arquitetura sugerida para evolução: separar coletores por exchange, normalização e persistência, API pública autenticada e frontend. Adicionar PostgreSQL para usuários, preferências, watchlists e alertas; fila/cache apenas quando carga exigir; serviço de identidade e provedor de cobrança com webhooks idempotentes; controle de entitlement/limites no backend; observabilidade, gestão de segredos e deploy. Planos devem vender recursos/limites, não promessa de retorno.

## Próximas etapas

1. **Identidade e preferências:** autenticação e isolamento por conta antes de persistir watchlists, regras de alerta e configurações no servidor.
2. **Produto para assinantes:** limites e permissões aplicados no backend, provedor de cobrança, webhooks idempotentes, privacidade e monitoramento.
3. **Mais análise de fluxo:** avaliar retenção e uso real das barras agregadas; persistir dados adicionais do book somente após medir volume e capacidade.
4. **Expansão:** Bybit e outras fontes após revisar APIs, limites e licenças de redistribuição.

## Verificação de licença de terceiros

O gráfico usa TradingView Lightweight Charts (Apache 2.0). O link e o aviso de atribuição permanecem visíveis no painel do gráfico; mantenha também os avisos de terceiros se redistribuir o software.
