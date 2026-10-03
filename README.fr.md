# bc250-monitor

*[English version](README.md)*

Un tableau de bord thermique et de performance léger pour l'**AMD BC-250** (la carte
issue du minage, bâtie autour d'une APU de PS5 bridée), avec une **garde thermique**
intégrée pour les machines qui font tourner des LLM locaux avec [Ollama](https://ollama.com).

- **Aucune dépendance** — un seul fichier Python, bibliothèque standard uniquement, ~15 Mo de RAM
- Échantillonnage toutes les 3 s, stockage SQLite, 14 jours d'historique
- Tableau de bord web : températures, puissance GPU, fréquence GPU, régime et consigne PWM du ventilateur
- **Garde thermique** : alerte journalisée à 88 °C, **déchargement des modèles Ollama
  de la VRAM** à 93 °C — la source de chaleur est coupée sans arrêter aucun service
- Les pics ne sont jamais masqués : en vue large, chaque point garde le *maximum*
  de température et de puissance des échantillons qu'il regroupe
- Thèmes clair et sombre, pastilles d'état accessibles (icône + libellé, jamais la
  couleur seule) et vue des données en table

![Tableau de bord bc250-monitor](docs/screenshot.png)

> L'interface du tableau de bord est en anglais.

## Pourquoi

Le refroidissement d'origine du BC-250 est juste. En inférence soutenue, le SoC peut
dépasser 100 °C. Ses capteurs sont en plus répartis entre trois pilotes (`amdgpu`,
`k10temp` et le Super I/O Nuvoton). Cet outil les réunit sur une seule page, garde un
historique pour relier chaleur et charge, et intervient avant que ça devienne dangereux.

## Prérequis

- Un AMD BC-250 sous Linux avec `systemd` et Python ≥ 3.8
- Le Super I/O **Nuvoton NCT6686D** visible dans `hwmon`. Il n'est pas détecté par
  défaut : il faut forcer le pilote `nct6683` du noyau (lecture seule, ce qui suffit ici) :

  ```sh
  echo "options nct6683 force=true" | sudo tee /etc/modprobe.d/nct6683.conf
  echo "nct6683"                    | sudo tee /etc/modules-load.d/99-sensors.conf
  sudo modprobe nct6683 force=true
  ```

  Le pilote tiers [`nct6687d`](https://github.com/Fred78290/nct6687d) fonctionne aussi.
- Facultatif : Ollama sur la même machine, pour la garde thermique et l'affichage du modèle chargé.

Vérifier que les capteurs sont visibles :

```sh
sensors | grep -iE "edge|Tctl|TSI|fan2|PPT"
```

## Installation

```sh
git clone https://github.com/bcyr/bc250-monitor.git
cd bc250-monitor
sudo ./install.sh
```

Ouvrir ensuite `http://<adresse-du-bc250>:9095`.

Le service tourne sous un utilisateur dynamique non privilégié. Sa base est dans
`/var/lib/bc250-monitor/metrics.db`.

Pour l'essayer sans l'installer : `python3 monitor.py` (la base est alors créée à côté
du script).

## Configuration

Définir les variables dans `/etc/default/bc250-monitor`, puis lancer
`sudo systemctl restart bc250-monitor`.

| Variable | Défaut | Rôle |
|---|---|---|
| `BC250_MON_PORT` | `9095` | Port HTTP |
| `BC250_MON_BIND` | `0.0.0.0` | Adresse d'écoute — `127.0.0.1` pour rester local |
| `BC250_MON_INTERVAL` | `3` | Secondes entre deux échantillons |
| `BC250_MON_RETENTION_DAYS` | `14` | Durée d'historique |
| `BC250_MON_DB` | *(voir plus haut)* | Chemin de la base SQLite |
| `BC250_OLLAMA_URL` | `http://127.0.0.1:11434` | API Ollama |
| `BC250_GUARD` | `1` | `0` désactive la garde thermique |
| `BC250_GUARD_WARN` | `88` | Seuil d'alerte, °C |
| `BC250_GUARD_CRIT` | `93` | Seuil de déchargement, °C |

## Garde thermique

À chaque échantillon, la garde retient le plus chaud de GPU edge, CPU Tctl et SoC TSI.

- À partir de `BC250_GUARD_WARN`, elle journalise une alerte. Elle ne le refait pas
  avant que la température soit redescendue de 3 °C sous le seuil.
- À partir de `BC250_GUARD_CRIT`, pendant **2 échantillons consécutifs**, elle demande
  à Ollama de décharger **tous** les modèles (`keep_alive: 0`). Elle attend ensuite
  120 s avant de pouvoir se redéclencher.

Le déchargement libère le GPU presque immédiatement. La requête suivante recharge le
modèle, donc rien n'est à redémarrer. Les événements s'affichent dans le tableau de
bord et dans `journalctl -u bc250-monitor`.

La garde est un filet de sécurité, pas un substitut à un refroidissement adéquat.

## Capteurs

| Champ | Source |
|---|---|
| GPU edge, puissance (PPT), VDDGFX | hwmon `amdgpu` |
| Fréquence GPU (SCLK) | `pp_dpm_sclk` du périphérique DRM amdgpu |
| CPU Tctl | `k10temp` |
| SoC TSI, thermistances carte 14/15, régime fan2, consigne pwm2 | `nct6686` / `nct6687` |
| NVMe | `nvme` |

Les chemins `hwmon` sont résolus **par nom**, car la numérotation `hwmonN` change d'un
démarrage à l'autre. Ils sont résolus de nouveau si un capteur disparaît, par exemple
après le rechargement d'un module. Un capteur absent s'affiche simplement `—`.

## API

Tous les points d'accès sont en `GET` et renvoient du JSON.

| Point d'accès | Contenu |
|---|---|
| `/api/current` | Un échantillon frais, les modèles Ollama chargés, l'uptime |
| `/api/history?range=15m\|1h\|6h\|24h\|7d` | Jusqu'à 420 points par série |
| `/api/events` | Les 50 derniers événements de la garde |
| `/api/health` | État de la garde, seuils, intervalle, rétention |

## Sécurité

Le tableau de bord n'a **aucune authentification** et écoute par défaut sur toutes les
interfaces. Il n'expose que de la télémétrie en lecture, et son API ne peut rien
modifier sur la machine. Il vaut quand même mieux le garder sur un réseau de confiance,
ou définir `BC250_MON_BIND=127.0.0.1` et le placer derrière un reverse proxy, ou
filtrer le port.

## Voir aussi

- [Documentation AMD BC-250](https://elektricm.github.io/amd-bc250-docs/) — matériel, capteurs, ventilation, governor GPU
- [bc250-ollama-openwebui](https://github.com/thelamer/bc250-ollama-openwebui) — installation d'Ollama + Open WebUI sur le BC-250

## Licence

[MIT](LICENSE)
