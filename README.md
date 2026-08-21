# Enexis kabel WFS ↔ CSV koppeling voor QGIS 4.2

QGIS Processing-plugin die Enexis `e_lv_map_cable`-kabellijnen **strikt 1-op-1** koppelt aan rijen uit een CSV-export en gekoppelde kabels naar DXF kan exporteren.

## v0.13.0: herbruikbare CSV-index en veiligere landelijke verwerking

De landelijke CSV wordt vanaf v0.13.0 niet meer bij iedere run opnieuw volledig geparset. De plugin bouwt één keer een herbruikbare SQLite-index op lokale schijf en controleert bij iedere volgende run of de bron-CSV nog dezelfde is.

De index wordt ongeldig verklaard en automatisch opnieuw opgebouwd wanneer onder andere bestandsgrootte, wijzigingstijd of de hash van het begin/einde van de CSV verandert.

### Waarom dit vooral voor kleine extents helpt

Bij een schermextent werkt de route nu als volgt:

1. de WFS levert binnen de extent alleen het labelattribuut;
2. de plugin bepaalt de unieke genormaliseerde WFS-labels;
3. alleen die labels worden via de SQLite-index uit de landelijke CSV gelezen;
4. alleen labels die werkelijk in WFS én CSV voorkomen krijgen een geometrie-opvraag;
5. daarna volgt de bestaande strikte 1-op-1 lengtematching.

Na de eerste indexbouw hoeft een kleine extent dus niet meer iedere keer circa twee miljoen CSV-regels te doorlopen.

## CSV-index/cachemap

De Processing-tool heeft nu correct de parameter:

**CSV-index/cachemap op lokale SSD (aanbevolen; wordt hergebruikt)**

Kies bij voorkeur een vaste map op een lokale SSD. Laat je het veld leeg, dan gebruikt de plugin een map onder de tijdelijke map van het besturingssysteem. Die kan door Windows worden opgeschoond, waardoor de index later opnieuw gebouwd moet worden.

De herbruikbare index bevat:

- CSV-rijnummer;
- genormaliseerde `Kabel Subgroep`;
- geparste kaartlengte;
- eventuele lengtefout;
- de oorspronkelijke CSV-waarden voor de output;
- een index op het genormaliseerde label.

Run-specifieke velden zoals `matched` en `wfs_found` staan **niet** in de vaste index. Voor een landelijke run wordt eerst een tijdelijke werkkopie gemaakt. Alleen die werkkopie krijgt matchstatus en tijdelijke WFS-geometrieën. Daardoor kan een geannuleerde of mislukte run de herbruikbare CSV-index niet vervuilen.

## Extentmodus

Kies bij **Beperk WFS tot scherm/gebied** bij voorkeur **Use current map canvas extent**.

De extent wordt naar **EPSG:28992 (RD New)** omgerekend.

De eerste WFS-opvraag bevat alleen het gedetecteerde labelveld en geen geometrie. Er worden maximaal 10.000 kabeldelen en maximaal 4 MB labeldata geaccepteerd. Daarna wordt alleen voor labels die ook in de CSV-index voorkomen geometrie opgehaald.

De geometrie-opvraag combineert de ruimtelijke begrenzing en het labelfilter in één CQL-filter:

`BBOX(geografischeligging, ...) AND label IN (...)`

Per geometriebatch worden maximaal 10 gezamenlijke labels, maximaal 1.000 features en maximaal 8 MB verwerkt.

## Landelijke modus

Laat **Beperk WFS tot scherm/gebied** leeg om heel Nederland te verwerken.

De landelijke route is:

1. herbruikbare CSV-index openen of eenmalig bouwen;
2. de vaste index naar een tijdelijke SQLite-werkkopie op dezelfde lokale schijf kopiëren;
3. WFS één keer paginagewijs lezen in stabiele `fid`-volgorde;
4. alleen WFS-kabels waarvan het label in de CSV-index staat in de werkkopie bewaren;
5. maximaal 50 gezamenlijke labels tegelijk vanaf schijf laden;
6. per label strikt 1-op-1 op lengte matchen;
7. resultaten rechtstreeks naar de gekozen output schrijven;
8. tijdelijke landelijke werkkopie verwijderen; de vaste CSV-index blijft bestaan.

De WFS-paginagrootte is standaard 10.000 features en maximaal 64 MB. Bij een te grote pagina verlaagt de plugin de paginagrootte. Tijdelijke HTTP 429/5xx- en netwerkfouten worden maximaal drie keer opnieuw geprobeerd.

### Belangrijke veiligheid

Landelijke modus weigert nu `TEMPORARY_OUTPUT` en `memory:` voor beide grote outputs. Kies expliciet bestanden op lokale schijf, bij voorkeur GeoPackage. Zo kan QGIS niet alsnog miljoenen outputfeatures in RAM proberen te houden.

Als Processing wordt geannuleerd tijdens WFS-download, matching of het schrijven van niet-gekoppelde rijen, stopt de plugin met een foutmelding. Een gedeeltelijke download wordt niet meer stil als een complete landelijke analyse behandeld.

Voor een landelijke run:

- gebruik een lokale SSD voor de CSV-index/cachemap;
- gebruik GeoPackage-uitvoer op lokale SSD;
- houd minimaal ongeveer 5 GB vrije ruimte beschikbaar naast de vaste CSV-index;
- verwacht dat de bijna twee miljoen WFS-geometrieën de totale doorlooptijd bepalen.

## CSV

De CSV moet minimaal bevatten:

- `Kabel Subgroep`
- `Lengte [kaart] (m)`

Lengtes zoals `195`, `16,5`, `196,11` en `196.11` worden ondersteund.

## Koppelregels

1. `Kabelgroup: WLR1760-03` wordt genormaliseerd naar `WLR1760-03`.
2. Daarna moet de waarde exact gelijk zijn aan CSV `Kabel Subgroep`.
3. WFS-lengte wordt in RD New in meters berekend en op twee decimalen afgerond.
4. Binnen dezelfde exacte kabelgroep wordt strikt 1-op-1 gematcht.
5. Bij dubbele labels wordt de totale absolute lengte-afwijking geminimaliseerd.
6. Iedere WFS-lijn en iedere CSV-rij wordt maximaal één keer gebruikt.

## DXF

**Split gekoppelde kabels naar DXF (V6 - landelijk)** ondersteunt zowel selectie/zoekradius als landelijke streaming.

In landelijke streamingmodus worden alleen benodigde attributen gelezen, geometrieën direct naar DXF geschreven en standaard na 25.000 kabels een nieuw DXF-deel gestart. Lijnen worden in deze modus bewust niet samengevoegd om het RAM-gebruik begrensd te houden.

## QGIS-versie

De plugin is gericht op **QGIS 4.2.0 / Qt6**.

## Installatie / testen

1. Sluit QGIS na een eerdere vastloper of crash.
2. Start QGIS 4.2.0 opnieuw.
3. Verwijder de oude pluginversie.
4. Installeer de repository-ZIP van de gewenste versie.
5. Controleer dat **versie 0.13.0** actief is.
6. Kies een vaste lokale SSD-map voor **CSV-index/cachemap**.
7. Test eerst een kleine schermextent.
8. Controleer in het Processing-log `Extent-scan`, `CSV-index hergebruikt/nieuw gebouwd`, het aantal gezamenlijke labels en het aantal koppelingen.
