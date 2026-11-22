# Audit du code RS485_COM_tester V5.1

Revue ligne par ligne de `RS485_COM_tester_V.5.1.py` et du guide `Help-RS485-App.pdf`.
Chaque point indique **ce que fait réellement le code** et **pourquoi le résultat affiché n'est pas fiable**.
Tous ces points sont corrigés dans la V6 (`RS485_COM_tester_V6.py` + dossier `rs485tester/`).

## 1. Mesures fausses (le résultat affiché ne mesure pas ce qu'il annonce)

| # | Test V5.1 | Constat | Conséquence |
|---|-----------|---------|-------------|
| 1 | **Mesurer la latence** | Utilise `time.time()`. Sous Windows, sa résolution est de **15,625 ms** (tick système à 64 Hz). | Le « 15,63 ms » du guide est exactement **un tick d'horloge**, pas une latence. Le « parfois 0 ms » est le cas où le début et la fin tombent dans le même tick. Le résultat n'a pas de sens physique. |
| 2 | **Mesurer la bande passante** | À 9600 bauds en 8N1, 1 octet = 10 bits, soit **960 o/s au maximum**. Le code envoie 1024 o, attend 0,1 s, lit 1024 o (timeout 1 s), boucle pendant 5 s puis divise par **5 s fixes**, sans mesurer le temps réel. Il faut 1,067 s pour passer 1024 o sur la ligne : la boucle fait 5 tours en environ 5,33 s, et 5 × 1024 / 5 = **1024,0**. | Le « 1024 o/s attendus » du guide est **physiquement impossible** à 9600 bauds. Le test réussit par coïncidence arithmétique. Le critère `== 1024` (égalité stricte sur un flottant) échoue dès que le timing change un peu. |
| 3 | **Simuler des perturbations** | Envoie la chaîne de **caractères ASCII** `"01010101…"`, c'est-à-dire les octets 0x30 et 0x31. Ce n'est ni un motif de bits alterné (ce serait 0x55) ni une perturbation. | Ce n'est qu'un test d'écho de 1 Ko. Aucune perturbation n'est simulée. |
| 4 | **Tester le circuit complet** (délai R→E) | Avant de relire, le code fait `time.sleep(0.1)`, **puis** attend le délai saisi. Le délai réel entre la fin de la trame A→B et la réémission B→A est donc **≥ 100 ms**, quel que soit le délai saisi (même 0,001). Sous Windows, `sleep(0.001)` dure aussi jusqu'à 15,6 ms. | Le test **ne peut pas** révéler un module lent à basculer de récepteur à émetteur, sauf s'il met plus de 100 ms. Il ne mesure aucun temps de basculement. |
| 5 | **Toutes les vitesses** | Les deux ports sont réglés sur la même valeur demandée. Si le driver arrondit (ex. 1 100 000 → 1 090 909 sur FTDI), **les deux côtés arrondissent pareil**, donc le test réussit même si le débit réel n'est pas celui affiché. Les chaînes envoyées sont alphanumériques uniquement : aucun 0x00, 0xFF, 0x55, aucun octet > 0x7F. | « Succès à 1,1 Mbauds » ne prouve pas que la ligne a tourné à 1,1 Mbauds. Les motifs les plus exigeants pour la transmission (longues suites de 0 ou de 1, alternances rapides) ne sont jamais testés. |
| 6 | **Vitesse spécifique** | `octets/s = bauds / 8` : faux en 8N1 (10 bits/octet), il faut `bauds / 10`. Unité « µ/s » au lieu de µs. | Valeurs affichées fausses de 25 %. |

## 2. Bugs qui plantent ou faussent un test

| # | Endroit | Bug |
|---|---------|-----|
| 7 | `test_connectivity`, `test_round_trip` | `self.message_queue` n'existe pas. Au moindre `SerialException` (port occupé, débranché, même port A et B), le test plante avec `AttributeError`. |
| 8 | `test_round_trip` | Si le délai saisi n'est pas un nombre, `delay` n'est pas défini et le test plante (`NameError`). |
| 9 | `Stringalea_test` / `lire_chaine` | `caractere.decode()` est appelé octet par octet. Un octet ≥ 0x80 (bruit, mauvais débit) lève `UnicodeDecodeError`, ce qui **interrompt tout le balayage** au premier débit en erreur, puis provoque un `NameError` dans le gestionnaire d'erreur. Les ports restent ouverts. |
| 10 | `lire_chaine` | La lecture s'arrête au premier `\n` : une trame binaire contenant 0x0A est tronquée. |
| 11 | `Stringalea_test` | `all_tests_passed = False` sur un compteur entier. Le total « /21 » est codé en dur. |
| 12 | `Stringalea_test_spe` | Saisie invalide : `baud_rate` n'est pas défini, d'où un `NameError`. |
| 13 | Threads | Les boutons lancent les tests **dans le thread de l'interface** (fenêtre figée). « Lancer tous les tests » appelle Tkinter **depuis un thread secondaire**, ce qui n'est pas thread-safe et provoque des plantages aléatoires. |
| 14 | `envoyer_chaine` | Bascule RTS avec 2 × 100 ms d'attente sans option. Cela ajoute 200 ms et n'a d'effet que sur des adaptateurs sans contrôle automatique du sens. |
| 15 | `validate_ports` | Ne vérifie pas que A ≠ B. Le même port deux fois donne une erreur d'ouverture, puis le plantage du point 7. |
| 16 | `auto_update_ports` | Réécrit les listes de ports chaque seconde, ce qui peut refermer la liste déroulante ouverte. Le texte par défaut est incohérent (« Select Port » / « Sélectionnez un port »). |
| 17 | Aide PDF | Chemin relatif et PDF **non intégré** à l'exe (`datas=[]`) : l'aide ne s'ouvre que si l'exe est lancé depuis son dossier. |
| 18 | Tampons | Les tampons RX ne sont pas vidés avant chaque test : des octets résiduels d'un test précédent peuvent fausser le suivant. |

## 3. Ce qui manquait pour un vrai banc de test RS485
- Comparaison **octet par octet et bit par bit** de l'entrée et de la sortie, avec la position des erreurs.
- Test dans **les deux sens** (A→B et B→A).
- Motifs binaires de stress (0x00, 0xFF, 0x55, 0xAA, 256 valeurs, PRBS) et trames typiques (Modbus RTU avec CRC).
- **Envoi continu** pour l'oscilloscope, avec vérification en temps réel et compteur d'erreurs.
- Mesure du **taux d'erreur binaire (BER)** sur longue durée, avec sa borne de confiance.
- Réglage du format (bits de données, parité, bits de stop).
- Mesure du temps de **retournement** (balayage du délai pour trouver le minimum qui fonctionne).
- Débit **réel estimé** par l'adaptateur (FTDI) et rappel des valeurs à contrôler à l'oscilloscope (durée d'un bit, fréquence de 0x55…).
- Rapport de test horodaté et traçable (n° de série du module, opérateur, configuration).
