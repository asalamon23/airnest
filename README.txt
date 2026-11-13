# AirNest – Smart Ventilation Dashboard

**A modern, local-first monitoring and automation dashboard for Dantherm ventilation systems.**

AirNest provides a user-friendly interface for monitoring and managing compatible Dantherm heat recovery ventilation units over a local network using Modbus TCP.

Designed as an alternative to basic manufacturer interfaces, AirNest combines real-time sensor readings, ventilation controls, customizable automation, historical monitoring, and a visual representation of airflow throughout the home.

## Features

### Real-Time Monitoring

Monitor ventilation performance through a modern web dashboard:

- Outdoor, supply, extract, and exhaust air temperatures
- Indoor relative humidity
- Supply and extract fan speeds
- Operating mode and ventilation level
- Heat exchanger bypass status
- Filter replacement countdown
- Additional available Modbus sensor data

### Ventilation Controls

Manage ventilation directly from the dashboard:

- Manual fan speed selection (levels 1–4)
- Sleep mode for temporary low-speed operation
- Vacation mode for reduced ventilation
- Night mode with configurable operating hours

Control functionality depends on the connected unit and its supported Modbus registers.

### Smart Automation

Create customized ventilation behavior based on schedules and environmental conditions.

**Daily scheduling**

Configure different ventilation speeds throughout the day, including separate settings for morning, daytime, evening, and nighttime operation.

**Humidity-based automation**

Automatically adjust ventilation when indoor humidity crosses configurable thresholds.

For example:

- Reduce ventilation to level 1 below 42% relative humidity.
- Resume scheduled ventilation when humidity reaches 45%.

Hysteresis helps prevent frequent fan-speed switching.

### Visual Airflow Dashboard

AirNest includes a graphical representation of a heat recovery ventilation system, illustrating four main airflow paths:

- **Blue:** Fresh outdoor air entering the ventilation system
- **Teal:** Conditioned fresh air supplied to indoor rooms
- **Orange:** Indoor air extracted toward the ventilation unit
- **Grey:** Exhaust air discharged outdoors

The illustration helps users understand how heat recovery ventilation works.

### Maintenance and Diagnostics

- Filter replacement reminders
- Configurable filter maintenance intervals
- Connection health monitoring
- Error logging and diagnostics
- Raw Modbus register inspection
- Historical measurements and CSV export

## Technology

AirNest uses a lightweight local architecture:

| Component | Technology |
|---|---|
| Backend | Python |
| Communication | Modbus TCP/IP |
| Dashboard | HTML, CSS, JavaScript |
| Data storage | Local files and CSV |
| Windows application | PyInstaller |
| Build automation | GitHub Actions |

## Getting Started

### Requirements

- A compatible Dantherm ventilation unit
- Modbus TCP connectivity
- A Windows computer connected to the same local network
- Access to the ventilation unit's local IP address

### Installation

1. Download the latest Windows build from the GitHub Actions artifacts or an available release.
2. Extract the downloaded archive.
3. Launch the AirNest executable.
4. Open the dashboard in your browser at `http://localhost:8088`.
5. Configure the ventilation unit's IP address if required.

No cloud account or internet connection is required for normal local operation.

## Compatibility

AirNest is primarily developed for the **Dantherm HCV 400** ventilation unit.

Other Dantherm models may be compatible if they expose similar Modbus TCP registers, but their register mappings and supported controls must be verified before use.

## Safety and Limitations

AirNest is an independent, community-developed project and is not affiliated with or endorsed by Dantherm.

Some device-control functions rely on community-documented Modbus register mappings and have not been validated across all firmware versions.

Users should verify compatibility before enabling write operations or automation. The dashboard does not replace manufacturer safety controls, recommended ventilation requirements, or regular equipment

![AirNest Dashboard](house_airflow.png)
