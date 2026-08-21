# Enexis kabel WFS ↔ CSV koppeling voor QGIS 4.2

QGIS Processing-plugin die Enexis `e_lv_map_cable`-kabellijnen **strikt 1-op-1** koppelt aan rijen uit een CSV-export.

## Gebruik v0.7.0 of nieuwer

Vanaf **v0.7.0** werkt de schermextent-modus bewust eenvoudiger:

1. QGIS geeft de gekozen schermextent door.
2. De plugin haalt **alleen de WFS-kabels binnen die extent** op.
3. De WFS-opvraag gebruikt in deze modus géén CSV-labels en géén groot CQL-labelfilter.
4. Pas nadat de kabels uit het schermgebied lokaal beschikbaar zijn, worden hun `label` en kaartlengte vergeleken met de CSV.
5. Daarna wordt binnen iedere exacte `Kabel Subgroep` de 1-op-1 lengtematching uitgevoerd.

Dat betekent dus letterlijk:

**schermextent → WFS-kabels ophalen → lokaal CSV vergelijken → 1-op-1 koppelen**

## Waarom deze aanpak

De eerdere versies probeerden de WFS tegelijk op schermextent én CSV-labels te filteren. Dat maakte de request complexer en bleek in de praktijk niet stabiel genoeg.

Voor een kleine kaartcanvas-extent is het veel eenvoudiger om eerst alleen de kabels in beeld op te halen en daarna lokaal te bepalen welke daarvan in de CSV staan.

## Low-resource ontwerp

De plugin gebruikt geen live QGIS WFS-laag en geen parallelle WFS-downloads.

Veiligheidsgrenzen:

- maximaal **1 WFS-request tegelijk**;
- bij extent-modus maximaal **500 kabels** in de response;
- maximaal **8 MB** per GetFeature-response;
- metadatarequests maximaal **2 MB**;
- wordt een grens overschreden, dan stopt de tool gecontroleerd en vraagt hij om verder in te zoomen.

Er worden bovendien alleen het WFS-`label` en de geometrie opgevraagd wanneer het geometrieveld bekend is. Overige WFS-attributen worden niet meegenomen.

## Schermextent

Kies bij **Beperk WFS tot scherm/gebied** bij voorkeur **Use current map canvas extent**.

De extent wordt naar **EPSG:28992 (RD New)** omgerekend en rechtstreeks als WFS `bbox` verstuurd.

Belangrijk: de CSV wordt in deze modus **niet** gebruikt om de WFS-download te bepalen. Daardoor is de netwerkquery klein en voorspelbaar.

Als meer dan 500 kabels binnen de gekozen extent vallen, stopt de plugin vóórdat een grote response het geheugen vult. Zoom dan verder in.

## Zonder extent

Zonder extent blijft een veilige fallback bestaan. Dan worden de CSV-labels in kleine batches van maximaal 5 Kabel Subgroepen per request opgevraagd. Zonder extent worden maximaal 20 geldige kabelgroepen toegestaan.

## Automatische WFS-laag

Je hoeft geen WFS-laag te kiezen.

De plugin zoekt automatisch een featuretype waarvan de naam `e_lv_map_cable` bevat. De gevonden volledige typename en het geometrieveld worden in QGIS-instellingen gecachet, zodat volgende runs deze metadata normaal niet opnieuw hoeven op te halen.

## CSV

De CSV moet minimaal bevatten:

- `Kabel Subgroep`
- `Lengte [kaart] (m)`

Lengtes als `195`, `16,5`, `196,11` en `196.11` worden ondersteund.

## Koppelregels

1. `Kabelgroup: WLR1760-03` wordt genormaliseerd naar `WLR1760-03`.
2. Daarna moet het label **exact** gelijk zijn aan CSV `Kabel Subgroep`.
3. De WFS-lengte wordt als kaartlengte in RD New berekend en op 2 decimalen afgerond.
4. Binnen dezelfde exacte Kabel Subgroep wordt strikt 1-op-1 gematcht op de best passende lengte.
5. Iedere WFS-lijn en iedere CSV-rij wordt maximaal één keer gebruikt.
6. WFS-kabels in de extent waarvan het label niet in de CSV staat krijgen `GEEN_EXACT_LABEL_IN_CSV`.
7. CSV-regels waarvoor binnen de gekozen extent geen kabel met dat label is gevonden krijgen `GEEN_MATCH_BINNEN_EXTENT`.

## QGIS-versie

De plugin is gericht op **QGIS 4.2.0 / Qt6** en gebruikt de QGIS 4 API (`QMetaType.Type.*`, `Qgis.WkbType.*` en `QgsFeatureSink.Flag.FastInsert`).

## Installatie / testen

1. Sluit QGIS als een oudere pluginversie eerder is vastgelopen of gecrasht.
2. Start QGIS 4.2.0 opnieuw.
3. Verwijder de oude pluginversie.
4. Installeer de nieuwste repository-ZIP.
5. Controleer dat **versie 0.7.0** actief is.
6. Open **Processing → Toolbox → Enexis → Kabelkoppeling → Koppel Enexis WFS-kabels automatisch aan CSV (extent-first)**.
7. Kies de CSV en **Use current map canvas extent**.
8. Test eerst met een klein kaartvenster.
