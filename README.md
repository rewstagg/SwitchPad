# SwitchPad

A native app for controlling [SwitchBot](https://www.switch-bot.com/) devices from the **Purism Librem 5** and other Linux phones running Phosh. It's built with GTK 4 and libadwaita, so it fits the phone's look, adapts to the small screen, and also works in a normal desktop window when the phone is docked.

SwitchPad controls devices through the official SwitchBot Cloud API. It also controls **Bots directly over Bluetooth** when the phone is near them, which is faster and works without the hub or the internet.

> SwitchPad is an independent project and isn't affiliated with or endorsed by SwitchBot / Woan Technology.

## Features

- **All your devices in one list**, each with a quick control in its row: on/off switches, Press for Bots, open/close for curtains, lock/unlock for locks
- **Detail page per device** with full controls (brightness, colour, white temperature, curtain position, vacuum start/stop/dock) and everything the device reports (battery, temperature, humidity, and so on)
- **Bots show only the control for their mode:** Press in press mode, an on/off switch in switch mode
- **Bluetooth control of Bots** when in range, with live status from the Bot's own broadcasts and automatic fallback to the cloud when it's out of range
- **Infrared remotes** through a SwitchBot hub: power, TV volume and channel, fan and air-conditioner controls, and any button you taught in the SwitchBot app
- **Scenes:** run any scene you made in the SwitchBot app with one tap
- **Unlocking a door always asks you to confirm first**
- **Credentials stored in the GNOME keyring**, never in plain text where a keyring is available
- **No extra dependencies:** uses only packages already on PureOS

## Requirements

- Librem 5 with PureOS 11 (Crimson), or another Linux system with:
  - Python 3.11+
  - GTK 4.8+ and libadwaita 1.2+ with GObject introspection
  - libsecret (for the keyring)
  - BlueZ (optional, for Bluetooth)
- A SwitchBot account, with a hub for devices that don't have Wi-Fi

On PureOS/Debian these are all installed by default on the Librem 5. Elsewhere:

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-secret-1 bluez
```

## Install

```sh
git clone https://github.com/rewstagg/SwitchPad.git
cd SwitchPad
./install.sh
```

This installs for your user only (no root needed). SwitchPad appears in the app grid, and you can also run it with `switchpad`.

The installer copies the app into `~/.local/share/switchpad`, so after pulling updates or editing `switchpad.py`, run `./install.sh` again.

To remove it, run `./uninstall.sh`.

## Setup: get your token and secret

SwitchPad needs your SwitchBot API token and secret key, which you get from the official SwitchBot app:

1. Open the SwitchBot app and go to **Profile → Preferences → About**.
2. Tap **App Version** 10 times. **Developer Options** appears.
3. Open **Developer Options** and copy the **Token** and **Secret Key**.
4. In SwitchPad, tap **Set Up** (or **Menu → Settings**), paste both in, and tap **Save and Connect**.

Each device must have **Cloud Service** turned on in the SwitchBot app to be controlled through the cloud.

## Bluetooth

When **Menu → Settings → Use Bluetooth when nearby** is on (the default), SwitchPad listens for Bot broadcasts while it's open:

- A Bot that's in range shows a Bluetooth icon in the list. Its page shows **Connection: Bluetooth** with the signal strength.
- Press, On and Off go straight to the Bot, without the hub or the internet, and don't count toward the cloud API's daily limit.
- If a Bluetooth command fails, SwitchPad sends it through the cloud instead and shows "Bluetooth failed, sent via cloud". To see why it failed, run `switchpad` from a terminal.
- Scanning only runs while SwitchPad is in the foreground, to save battery.

Bluetooth range is roughly 10 m. Bots with a password set in the SwitchBot app use encrypted Bluetooth, which isn't supported yet; those are always controlled through the cloud.

## Supported devices

| Device | Cloud | Bluetooth | Tested on hardware |
|---|---|---|---|
| Bot | Press, on/off | Press, on/off, live status | ✅ |
| Plug, Plug Mini, Relay Switch | On/off | — | |
| Color Bulb, Floor Lamp | On/off, brightness, colour, white temperature | — | |
| Strip Light | On/off, brightness, colour | — | |
| Ceiling Light | On/off, brightness, white temperature | — | |
| Curtain, Curtain 3 | Open, close, pause, position | — | |
| Roller Shade, Blind Tilt | Open, close (Roller Shade: position) | — | |
| Lock, Lock Pro | Lock, unlock (with confirmation) | — | |
| Robot vacuums | Start, stop/pause, dock | — | |
| Humidifier, fans, air purifiers | On/off | — | |
| Meter, Meter Plus, Outdoor Meter, Hub 2 | Temperature, humidity, battery | — | |
| Contact, motion and other sensors | Status | — | |
| IR remotes (through a hub) | Power, TV/fan/AC controls, custom buttons | — | |
| Scenes | Run | — | |

Only Bots have been tested on real hardware so far. The others follow SwitchBot's API documentation. If something doesn't work with your device, please open an issue with the device model and the error shown.

## Privacy

- Your token and secret are stored in the GNOME keyring. If no keyring is available, they go in `~/.config/switchpad/credentials.json`, readable only by you.
- The device list is cached in `~/.cache/switchpad/devices.json` so the app opens instantly.
- SwitchPad talks only to `api.switch-bot.com` and, over Bluetooth, to your own devices. Nothing else is sent anywhere.

## Limits

- The SwitchBot Cloud API allows 10,000 requests per day per account. Normal use stays far below that, and Bluetooth commands don't count.
- Cloud status can lag the device by a few seconds.

## Development

The whole app is a single Python file, `switchpad.py`. Run it straight from the source folder without installing:

```sh
./switchpad.py
```

References:
- [SwitchBot Cloud API v1.1](https://github.com/OpenWonderLabs/SwitchBotAPI)
- [SwitchBot Bluetooth API](https://github.com/OpenWonderLabs/SwitchBotAPI-BLE)
