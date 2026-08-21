# Enexis kabel WFS ↔ CSV koppeling voor QGIS 4.2

QGIS Processing-plugin die Enexis `e_lv_map_cable`-kabellijnen **strikt 1-op-1** koppelt aan rijen uit een CSV-export.

## Gebruik v0.6.0 of nieuwer

Versies vóór v0.6.0 konden bij grotere WFS-responses te veel RAM en CPU gebruiken. Vooral v0.5.0 kon meerdere volledige GeoJSON-responses tegelijk in geheugen houden en daarna dezelfde gegevens nogmaals als QGIS-features bewaren.

**v0.6.0 is daarom bewust een low-resource versie.** Stabiliteit gaat vóór maximale downloadsnelheid.

## Vereisten

- QGIS **4.2.0** of nieuwer binnen de 4.x-reeks.
- CSV met minimaal:
  - `Kabel Subgroep`
  - `Lengte [kaart] (m)`

De code gebruikt de QGIS 4 / Qt6 API (`QMetaType.Type.*`, `Qgis.WkbType.*` en `QgsFeatureSink.Flag.FastInsert`).

## Low-resource ontwerp

De plugin gebruikt geen live QGIS WFS-laag en geen QGIS WFS-background downloader. Daarnaast zijn vanaf v0.6.0 alle parallelle WFS-downloads verwijderd.

Per run gelden harde veiligheidsgrenzen:

- **maximaal 1 HTTP/WFS-request tegelijk**;
- **maximaal 5 Kabel Subgroepen per WFS-request**;
- **maximaal 500 WFS-features per batch**;
- **maximaal 8 MB per GetFeature-response**;
- GetCapabilities is begrensd op **2 MB**;
- zonder extent worden maximaal **20 geldige Kabel Subgroepen** toegestaan.

Als een limiet wordt geraakt stopt Processing gecontroleerd met een melding om verder in te zoomen of een kleinere extent te kiezen. De plugin gaat dan niet verder met downloaden en vullen van RAM.

## Geheugengebruik

Een WFS-batch wordt volledig afgehandeld voordat de volgende start:

1. kleine `GetFeature`-request naar Enexis;
2. alleen het `label`-attribuut en de geometrie worden in QGIS geparsed;
3. lengtes worden berekend;
4. binnen iedere exacte Kabel Subgroep wordt 1-op-1 gematcht;
5. resultaten worden naar de Processing-output geschreven;
6. de complete batch wordt uit het Python-geheugen verwijderd en garbage collection wordt uitgevoerd;
7. pas daarna start de volgende WFS-batch.

Er staat dus niet meer een verzameling van meerdere grote GeoJSON-responses tegelijk in het geheugen.

## Schermextent

Gebruik bij voorkeur bij **Beperk WFS tot scherm/gebied** de optie voor de huidige kaartcanvas-extent.

- De extent wordt naar **EPSG:28992 (RD New)** omgerekend.
- De BBOX wordt direct naar de Enexis WFS-server gestuurd.
- De volledige geometrie van een kabel die de BBOX raakt wordt gebruikt voor de lengte; de lijn wordt niet op de schermrand afgeknipt.
- CSV-rijen zonder gevonden kabel binnen een actieve extent krijgen `GEEN_MATCH_BINNEN_EXTENT`.

Zonder extent is de plugin expres streng. Bij meer dan 20 geldige kabelgroepen stopt hij voordat een potentieel grote landelijke opvraag wordt gestart.

## Automatische WFS-laag

De gebruiker hoeft geen WFS-laag te selecteren.

De plugin probeert eerst rechtstreeks `e_lv_map_cable`. Werkt de ongekwalificeerde naam niet, dan wordt eenmalig `GetCapabilities` gebruikt om de volledige typename te vinden. De gevonden typename wordt in QGIS-instellingen opgeslagen zodat volgende runs deze stap normaal overslaan.

## Koppelregels

1. `Kabelgroup: WLR1760-03` wordt genormaliseerd naar `WLR1760-03`.
2. Daarna moet de waarde **exact** gelijk zijn aan `Kabel Subgroep` uit de CSV.
3. WFS-lengte wordt in RD New als kaartlengte in meters berekend en op 2 decimalen afgerond.
4. CSV-lengtes met bijvoorbeeld `195`, `16,5`, `196,11` en `196.11` worden ondersteund.
5. Binnen dezelfde exacte Kabel Subgroep is de toewijzing strikt 1-op-1.
6. Iedere WFS-lijn en iedere CSV-rij wordt maximaal één keer gebruikt.
7. Bij dubbele kabelgroepen wordt de totale absolute lengte-afwijking geminimaliseerd.

## Uitvoer

### Gekoppelde WFS-lijnen

De output bevat de kabelgeometrie, de CSV-kolommen met prefix `csv_` bij een match en onder andere:

- `match_status`
- `wfs_label_norm`
- `wfs_len_m`
- `csv_len_m`
- `len_diff_m`
- `csv_row_nr`

Om geheugen te besparen worden overige, voor de koppeling niet benodigde WFS-attributen vanaf v0.6.0 niet meer meegenomen.

### Niet-gekoppelde CSV-rijen

De tweede output bevat niet gebruikte CSV-regels met een reden, bijvoorbeeld `GEEN_MATCH_BINNEN_EXTENT`, `GEEN_EXACT_LABEL_IN_WFS`, `GEEN_WFS_LIJN_OVER_IN_LABELGROEP` of `ONGELDIGE_CSV_LENGTE`.

## Installatie / veilig testen

1. Sluit QGIS nadat een oudere versie is gecrasht.
2. Start QGIS 4.2.0 opnieuw.
3. Verwijder de oude pluginversie en installeer de nieuwste repository-ZIP.
4. Controleer in de pluginmanager dat **versie 0.6.0 of hoger** actief is.
5. Test eerst met een kleine kaartcanvas-extent.
6. Open **Processing → Toolbox → Enexis → Kabelkoppeling → Koppel Enexis WFS-kabels automatisch aan CSV (low-resource)**.
7. Kies de CSV en de huidige kaartcanvas-extent.

Gebruik v0.5.0 en ouder niet meer voor deze workflow.
