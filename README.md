# Aktiekurser – e-ink aktieticker

Visar kurser från Avanza på en Waveshare e-paper-display (Raspberry Pi). Vad som visas
(aktier, fonder, vyer, tid per vy, portfölj) styrs från en liten webbsida på samma enhet.

Byggt för Waveshare 4.26" (800×480), men drivrutinen kan bytas (`AKTIE_DRIVER`).

## Funktioner
- Kurser, dagsgraf och årsgraf för aktier, ETF:er och fonder (Avanza)
- Webbgränssnitt för att lägga till/ta bort instrument, välja vyer och bytestid
- Valfri portföljvy med antal och snittkurs (sparas bara lokalt)
- Valfritt lösenord för webbsidan – **av som standard**
- Förhandsvisning som PNG utan hårdvara (`AKTIE_DRIVER=preview`)

## Installation (Raspberry Pi OS)
```bash
git clone https://github.com/waveshareteam/e-Paper ~/e-Paper      # Waveshares bibliotek
git clone <den här repon> ~/aktiekurser-web
cd ~/aktiekurser-web
python3 -m venv venv
venv/bin/pip install -r requirements.txt
sudo raspi-config nonint do_spi 0                                   # aktivera SPI
```

Starta för hand:
```bash
venv/bin/python main.py
```
Första starten skapar `settings.json` med exempelaktierna i `stocks.txt.imported-seed`.
Öppna sedan `http://<pi-ip>` (port 80, annars 8080).

### Som tjänst (autostart)
```bash
sed "s#__USER__#$USER#; s#__APPDIR__#$PWD#g" aktiekurser-web.service.example | sudo tee /etc/systemd/system/aktiekurser-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now aktiekurser-web
```

## Inställningar
| Miljövariabel | Standard | Betydelse |
|---|---|---|
| `AKTIE_DRIVER` | `epd4in26` | `epd4in26`, `epd3in52`, `epd2in13v3` eller `preview` |
| `AKTIE_PORT` | `80` | Webbport (faller tillbaka på 8080) |
| `AKTIE_SETTINGS` | `./settings.json` | Sökväg till inställningsfilen |
| `AKTIE_WAVESHARE_LIB` | `~/e-Paper/RaspberryPi_JetsonNano/python/lib` | Waveshare-biblioteket |

`settings.json` innehåller dina innehav och ev. lösenordshash och ligger därför i `.gitignore`.

## Tester
```bash
AKTIE_DRIVER=preview venv/bin/python -m unittest discover -s tests -v
```

## Friskrivning
Inte affilierad med Avanza. Använder Avanzas publika webb-API, som kan ändras utan förvarning.
Kurser är inte investeringsrådgivning.
