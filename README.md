# Vigia do Checkout

Monitor que verifica, a cada 5 minutos, se a página de checkout está
funcional — não só se o servidor responde.

Checa duas camadas:

1. **A página**: status 200, HTML completo e as marcas do formulário,
   bloco de pagamento, botão de compra e configuração do gateway.
2. **Os arquivos**: baixa de verdade cada CSS/JS crítico pelo mesmo
   caminho que o navegador do cliente percorre (passando pela CDN).

A segunda camada existe porque um checkout pode devolver HTML 200
perfeito e mesmo assim estar inutilizável, quando um arquivo essencial
deixa de ser entregue pela CDN.

## Configuração

Nenhuma credencial fica no código. Defina em
*Settings → Secrets and variables → Actions*:

| Secret | Para quê |
|---|---|
| `VIGIA_TG_TOKEN` | token do bot do Telegram |
| `VIGIA_TG_CHAT`  | id do chat que recebe o alerta |

Variáveis de ajuste (no workflow): `VIGIA_URL`, `VIGIA_FALHAS`, `VIGIA_TIMEOUT`.

## Rodar localmente

```bash
python3 vigia.py --teste
```
