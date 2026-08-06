# CAS Rule Expiry Automation

Standalone project: [`cas_rule_expiry_automation/`](../../cas_rule_expiry_automation/).

WebSocket-first Closing Auction Session algo:

- **Tuesday** → NIFTY expiry  
- **Thursday** → SENSEX expiry  
- Configurable **lots** and OTM strike steps  
- KiteTicker `MODE_FULL` for lowest practical latency  
- Backtest replays ticks through the same WebSocket handler path  

```bash
cp cas_rule_expiry_automation/config.ini.example cas_rule_expiry_automation/config.ini
python -m cas_rule_expiry_automation
```

UI: <http://127.0.0.1:5030> (light theme).
