# RS485 COM Tester V6

Banc de test pour liaisons RS485. Configuration type : PC → adaptateur A → module 1 → fibre → module 2 → adaptateur B → PC.
Tout ce qui est émis sur un port est comparé, octet par octet et bit par bit, à ce qui ressort sur l'autre.

## Lancer
- **Exe** : `dist\RS485_COM_tester_V6.exe`, autonome (Python et bibliothèques intégrés).
- **Source** : `pip install -r requirements.txt`, puis `python RS485_COM_tester_V6.py`.
- **Auto-tests sans matériel** : `python RS485_COM_tester_V6.py --selftest`.

## Construire l'exe (sur un PC Windows)
Double-cliquez `build_exe.bat`. Le script crée un environnement isolé, installe les dépendances, lance les
auto-tests, puis produit `dist\RS485_COM_tester_V6.exe` avec PyInstaller (fichier `RS485_COM_tester_V6.spec`).

## Onglets
| Onglet | Rôle |
|---|---|
| **Tests** | 8 tests avec schéma, explication, repères oscilloscope, paramètres et tableau de résultats. Campagne des tests cochés et rapport HTML + CSV horodaté (opérateur, n° de série des modules, adaptateurs). |
| **Générateur continu** | Émission en boucle d'une trame (HEX, ASCII, assistant Modbus RTU, CRC/XOR/somme) à période réglable, avec vérification en temps réel sur l'autre port. Affiche le signal UART attendu et les réglages oscilloscope. |
| **Moniteur E/S** | Les deux ports ouverts, trames horodatées en HEX et ASCII, envoi manuel depuis A ou B avec comparaison automatique de la réception. |
| **Guide & schémas** | Fonctionnement de la chaîne, trame UART, niveaux RS485, retournement, BER, procédure oscilloscope, réglages Windows. |
| **Configuration** | Contrôle du sens (automatique ou RTS), écho local, liaison simulée SIM-A/SIM-B avec injection de défauts. |

## Les tests
1. **Connectivité A ↔ B** : trame courte dans les deux sens.
2. **Intégrité** : 256 valeurs, 0x00, 0xFF, 0x55/0xAA, bits baladeurs, PRBS-15.
3. **Modbus RTU** : requêtes et réponses réelles, CRC vérifié, silence t3,5.
4. **Retournement** : balayage du délai de réponse pour trouver le minimum fiable.
5. **Balayage des débits** : avec débit réel estimé pour les FTDI.
6. **Latence** : horloge haute résolution, statistiques et histogramme.
7. **Débit soutenu** : flux continu à 100 %, comparé au maximum théorique.
8. **Endurance / BER** : paquets numérotés + CRC, BER mesuré ou borne à 95 %.

## Points importants
- Adaptateurs FTDI : réglez le **Latency Timer à 1 ms** (Gestionnaire de périphériques → port → Avancé).
- Le PC voit les données à travers l'USB. Les niveaux, les fronts et les temps en µs se mesurent à l'oscilloscope :
  utilisez le générateur continu.
- `AUDIT_V5.1.md` liste les erreurs de mesure et les bugs de la V5.1, corrigés ici.

## Structure du code
```
RS485_COM_tester_V6.py     point d'entrée
rs485tester/core.py        réglages, timings, CRC, motifs, comparaison, ports
rs485tester/engine.py      tests, générateur, moniteur (sans interface)
rs485tester/simport.py     liaison simulée (validation du testeur)
rs485tester/diagrams.py    schémas
rs485tester/report.py      rapport HTML / CSV
rs485tester/gui.py         interface customtkinter
tests/test_engine.py       auto-tests
```
