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
- Trades agregados reais (aggTrades), classificação do lado agressor conforme campo `m` da Binance e deduplicação por ID. Um coletor em segundo plano acompanha os 25 pares sem depender de uma tela aberta, grava trades em Postgres ou SQLite com retenção configurável e expõe qualidade/latência em `/api/flow-quality`.
- Livro de ofertas com snapshot seguido de diffs de profundidade. O backend verifica a sequência de IDs e sincroniza novamente quando detecta gap. Exibe spread e OBI para a profundidade mostrada.
- CVD calculado somente da amostra visível de trades, com cobertura explicitamente marcada. O histórico de trades do coletor fica por um dia por padrão (`FLOW_RETENTION_DAYS` controla a retenção).
- Zonas diárias de volatilidade GARCH(1,1) e HV252 nos gráficos intradiários, com referência no último fechamento diário concluído, faixas lognormais e controles para ocultar cada conjunto. O candle diário ainda aberto é excluído.
- Sinais determinísticos de reversão BUY/SELL em bandas GARCH/HV252, confirmados por rejeição em candle fechado, filtro de volume (1,2× a média dos 20 candles prévios), score mínimo 50 e cooldown de 10 candles. Direções, fontes, sigma mínimo e tolerância de toque são configuráveis; marcadores aparecem no gráfico e nenhuma ordem é enviada.
- Sinal determinístico por candle fechado: EMA(9/21), RSI(14), volume relativo e rompimento de estrutura. Pesos indisponíveis continuam ausentes; pontuação não é normalizada nem apresentada como probabilidade.
- Funding, mark/index e open interest de Futures destacados como dados centrais do contrato. Fear & Greed via Alternative.me, com estado indisponível em falhas.
- Alertas de preço no navegador, armazenados localmente e sem serviço push.
- Dashboard responsivo, tema escuro, fuso America/Sao_Paulo e atribuição requerida pela licença de Lightweight Charts.

## Limites desta versão

- Footprint, replay e heatmap histórico ainda não estão implementados. O histórico do book começa quando o coletor inicia; esta versão não persiste snapshots e mantém estado apenas em memória.
- CVD e volume de trades no painel representam a janela/amostra observada, não todo o volume do candle nem histórico completo. A API pode buscar uma amostra REST recente para preencher a primeira tela; isso não equivale a cobertura contínua do coletor.
- O sinal não incorpora fluxo nem derivativos; os fatores aparecem como ausentes no motor. Não calcula entradas, stops ou alvos.
- Alertas cruzam somente o preço para cima, são por navegador e não sincronizam entre dispositivos.
- Sem `DATABASE_URL`, SQLite mantém os trades enquanto o arquivo existir. Configure `FLOW_DB_PATH` para escolher o caminho e `FLOW_STORAGE_DURABLE=true` somente se estiver em disco persistente. O Render gratuito não preserva esse arquivo entre reinícios/deploys.
- Para persistir sem custo recorrente, o backend aceita `DATABASE_URL` de um Postgres como o Supabase Free. Crie um projeto, copie no painel Supabase a URL **Session pooler** e cadastre-a como variável secreta `DATABASE_URL` no serviço `nexus-terminal-api` do Render; não coloque a senha no repositório. A API passa a indicar `storage=postgres` e `storageDurable=true`. O plano Supabase Free tem 500 MB e pode pausar projetos após uma semana com baixa atividade; monitore o uso exibido no painel.
- Não há usuário, login, cobrança, planos ou autorização por assinatura. O dashboard local valida experiência e dados antes da construção dessas partes.
- Não há backtest ou paper trading nesta fase. Nenhum módulo simula desempenho ou execução financeira.
- REST pública está sujeita a rate limits, disponibilidade regional e alterações da exchange. A aplicação reporta erros e não fabrica dados.

## Publicação e assinaturas

Trate o app atual como versão de validação local. Antes de disponibilizar dados em serviço pago, confirme por escrito os direitos de uso e redistribuição comercial dos endpoints, streams e datasets de cada exchange; dados acessíveis publicamente não significam automaticamente licença para redistribuição. Consulte também retenção, privacidade, termos de uso e obrigações locais. O repositório não oferece aconselhamento jurídico.

### Protótipo gratuito no Render

O arquivo `render.yaml` descreve dois Web Services Docker gratuitos: `nexus-terminal-web` (Next.js) e `nexus-terminal-api` (FastAPI). No painel do Render, crie um Blueprint a partir do repositório e confira os dois serviços e as variáveis de ambiente antes de aplicar. O `NEXUS_API_ORIGIN` é preenchido pela URL do serviço API. Se escolher outros nomes de serviço, atualize as URLs `*.onrender.com` correspondentes no `render.yaml`.

O Next.js lê `NEXUS_API_ORIGIN` durante o build, então a URL da API precisa estar disponível na criação/build do Blueprint. O serviço FastAPI responde na porta `PORT` do Render. A URL WebSocket pública usa `wss://`. Os serviços gratuitos podem dormir após 15 minutos sem tráfego recebido, demorar aproximadamente um minuto para acordar e não têm armazenamento persistente; são adequados para demonstração, não para disponibilidade prometida a assinantes. O Render pode suspender serviços sem forma de pagamento se os limites incluídos de uso ou tráfego forem excedidos.

Depois do primeiro deploy, confirme no painel que os domínios públicos correspondem aos nomes definidos, que `/health` da API retorna saudável e que `CORS_ORIGINS` contém a origem real do frontend. Em caso de renomeação, corrija `CORS_ORIGINS` e `NEXT_PUBLIC_MARKET_WS_URL` e faça novo deploy.

Arquitetura sugerida para evolução: separar coletores por exchange, normalização e persistência, API pública autenticada e frontend. Adicionar PostgreSQL para usuários, preferências, watchlists e alertas; fila/cache apenas quando carga exigir; serviço de identidade e provedor de cobrança com webhooks idempotentes; controle de entitlement/limites no backend; observabilidade, gestão de segredos e deploy. Planos devem vender recursos/limites, não promessa de retorno.

## Fases

1. **MVP local (esta entrega):** Binance Spot, 25 pares USDT, candles, trades, book, CVD observado, zonas de volatilidade com sinais de reversão explicáveis e alerta de preço.
2. **Coleta própria:** trades com retenção e cobertura declarada, SQLite local ou Postgres gratuito externo; replay e heatmap; footprint somente com trades suficientes.
3. **Análise:** indicadores, sinais com fatores adicionais e backtest sem look-ahead, custos/slippage e validação fora da amostra.
4. **Produto hospedado:** autenticação, equipes/contas, planos, billing, limites, privacidade, segurança, monitoramento e reconciliação de dados.
5. **Expansão:** Bybit e outras fontes após revisar APIs, limites e licenças de redistribuição.

## Verificação de licença de terceiros

O gráfico usa TradingView Lightweight Charts (Apache 2.0). O link e o aviso de atribuição permanecem visíveis no painel do gráfico; mantenha também os avisos de terceiros se redistribuir o software.
