#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
KONSOLOWY DASHBOARD CAN + STEROWANIE NA ŻYWO (v15 - DBC Fix: 0x461, 0x463, 0x464, 0x466, 0x468, 0x360, 0x362)
DLA FALOWNIKA NX TECHNOLOGIES W90
===============================================================================
Zakres funkcjonalności (v15):
  1. Konfiguracja połączenia PEAK PCAN-USB (500 kbps, python-can) identyczna
     jak w poprzedniej wersji + wybór trybu LIVE (PCAN-USB) lub OFFLINE (Symulacja).
  2. Sekwencja twardego resetu falownika (klawisz [R]).
  3. Ręczne wpisywanie żądanego stanu na App_State_Req.
  4. Bezpieczna obsługa zadawania momentu obrotowego.
  5. POPRAWKI DBC:
     - 0x461 (EMC_TX_STATE_2, DLC=7): Bitowe dekodowanie Intel (Little-Endian).
     - 0x463 (EMC_TX_STATE_4, DLC=8):
         * Current_D_A         : startbit 0,  len 16, Signed,   Factor 0.03125
         * Current_Q_A         : startbit 16, len 16, Signed,   Factor 0.03125
         * Volt_Modulus_permil : startbit 32, len 12, Unsigned, Factor 1
         * EMachine_Speed_erpm : startbit 44, len 20, Signed,   Factor 1
     - 0x360 (EMC_RX_SETPOINT_1, DLC=3) i 0x362 (EMC_RX_SETPOINT_3, DLC=4):
         * Zgodny z DBC układ bitowy (CRC, CNT, ZERO, App_State_Req/Flt_Clear, Torque_Nm_Req).
===============================================================================
"""

import os
import re
import sys
import time
import shutil
import struct
import argparse

# Obsługa nieblokującego odczytu klawiatury (Windows msvcrt / Linux select+termios)
if os.name == "nt":
    import msvcrt
else:
    import select
    import tty
    import termios

# ==============================================================================
# 0. INICJALIZACJA KONSOLI WINDOWS (OBSŁUGA KODÓW ANSI)
# ==============================================================================

def enable_windows_ansi():
    """Włącza obsługę sekwencji ANSI (VT100) w konsoli Windows CMD / PowerShell."""
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            os.system("")

enable_windows_ansi()

# Kody sterujące ANSI
C_RESET      = "\033[0m"
C_BOLD       = "\033[1m"
C_RED        = "\033[91m"
C_GREEN      = "\033[92m"
C_YELLOW     = "\033[93m"
C_CYAN       = "\033[96m"
C_MAGENTA    = "\033[95m"
C_WHITE      = "\033[97m"
C_GRAY       = "\033[90m"

CLEAR_SCREEN = "\033[2J\033[H"
CURSOR_HOME  = "\033[H"
CLEAR_LINE   = "\033[K"
CLEAR_BELOW  = "\033[J"
CURSOR_HIDE  = "\033[?25l"
CURSOR_SHOW  = "\033[?25h"

ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

def fit_line_to_terminal(text: str, max_vis_width: int) -> str:
    """Przycinanie tekstu (z zachowaniem kodów kolorów ANSI) do szerokości terminala."""
    if max_vis_width <= 10:
        return text + CLEAR_LINE
    vis_count = 0
    out = []
    idx = 0
    n = len(text)
    while idx < n:
        match = ANSI_ESCAPE_RE.match(text, idx)
        if match:
            out.append(match.group(0))
            idx = match.end()
        else:
            if vis_count < max_vis_width:
                out.append(text[idx])
                vis_count += 1
            idx += 1
    out.append(C_RESET)
    out.append(CLEAR_LINE)
    return "".join(out)

# ==============================================================================
# 1. BAZA BŁĘDÓW I STANÓW FALOWNIKA NX TECHNOLOGIES W90
# ==============================================================================

L1_PWRSTG_BITS = {
    1:   ("Alive",        "Hardware interface is functional",            "None"),
    2:   ("Enable",       "Power stage is enabled",                      "None"),
    4:   ("Undervoltage", "Undervoltage (primary or isolated side)",     "Freewheeling"),
    8:   ("Desat",        "Desaturation / Overcurrent",                  "Freewheeling"),
    16:  ("DtimeFlt",     "Deadtime violation",                          "Freewheeling"),
    32:  ("ExtFlt",       "External fault (KL30_AUX connection to 12V)", "Active Short Circuit"),
    64:  ("OCP",          "Phase overcurrent",                           "Freewheeling"),
    128: ("OVP_TH1",      "DCBus overvoltage - threshold 1",             "Freewheeling"),
    256: ("OVP_TH2",      "DCBus overvoltage - threshold 2",             "Active Short Circuit"),
}

L2_EMCTRL_BITS = {
    1:   ("Ready",          "Software module initialized",                          "None"),
    2:   ("PosFbFlt",       "Position feedback fault (błąd resolvera/enkodera)",    "Freewheeling"),
    4:   ("ASC",            "Active Short Circuit",                                 "ASC"),
    8:   ("Curr_Imbalance", "Output phase current imbalance",                       "Freewheeling"),
    16:  ("PwrStgFlt",      "Power stage (L1) fault",                               "Freewheeling"),
    32:  ("Loop_delocked",  "Control loop instability",                             "Freewheeling"),
    64:  ("PhCurrAcqFlt",   "Fault on phase current acquisition",                   "Freewheeling"),
    128: ("BusMeasAcqFlt",  "Fault on bus measurements acquisition",                "Freewheeling"),
}

L3_DEM_CODES = {
    0:  ("Brak błędów (OK)",              "System działa poprawnie",                                   "None"),
    1:  ("Lost msg",                      "Setpoint message is incorrect or not received",             "Freewheeling"),
    2:  ("DCBus Undervoltage",            "DCBus voltage dropped below threshold in active mode",      "Freewheeling"),
    3:  ("pwrstg Overtemperature",        "Przegrzanie stopnia mocy",                                  "Freewheeling"),
    4:  ("pwrstg Temp degradation",       "Ograniczenie mocy przez temperaturę stopnia mocy",          "Degrade"),
    5:  ("EMCtrl",                        "Błąd sterowania silnikiem (sprawdź tabelę L2)",             "Freewheeling"),
    6:  ("Task overrun",                  "Przekroczenie czasu wykonywania zadania procesora",         "Freewheeling"),
    7:  ("CAN1 BusOff",                   "Błąd magistrali CAN1 (Bus Off)",                            "None"),
    8:  ("Emachine overtemperature",      "Przegrzanie silnika elektrycznego",                         "Freewheeling"),
    9:  ("Phase current out-of-range",    "Prąd fazowy poza zakresem",                                 "Freewheeling"),
    10: ("Power stage temp out-of-range", "Czujnik temp. stopnia mocy poza zakresem",                  "Freewheeling"),
    11: ("DC Bus out-of-range",           "Napięcie DC Bus poza zakresem pomiarowym",                  "Freewheeling"),
    12: ("DP Overtemp",                   "Przegrzanie DP",                                            "Freewheeling"),
    13: ("DRV Overtemp",                  "Przegrzanie sterownika bramek (Driver)",                    "Freewheeling"),
    14: ("Aux. supply undervoltage",      "Zbyt niskie napięcie zasilania pomocniczego",               "Freewheeling"),
    15: ("Aux. supply overvoltage",       "Zbyt wysokie napięcie zasilania pomocniczego",              "None"),
    16: ("Overspeed",                     "Przekroczenie maksymalnej prędkości obrotowej",             "Freewheeling"),
    17: ("Speed degrade",                 "Ograniczenie od prędkości obrotowej",                       "Degrade"),
    18: ("Emachine temp degrade",         "Ograniczenie mocy od temperatury silnika",                  "Degrade"),
    19: ("Bad current offset",            "Błędna kalibracja offsetu prądowego",                       "Freewheeling"),
    20: ("Absenc Err1",                   "Błąd czujnika pozycji (Absenc Err1)",                       "Freewheeling"),
    21: ("Ext temp 1 out-of-range",       "Czujnik temperatury zewn. 1 poza zakresem (NTC1)",          "None"),
    22: ("Ext temp 2 out-of-range",       "Czujnik temperatury zewn. 2 poza zakresem (NTC2)",          "None"),
    23: ("PMIC not ready",                "Układ zasilania PMIC niegotowy",                            "Freewheeling"),
    24: ("E2E CRC fault",                 "Błąd sumy kontrolnej CRC (zabezpieczenie E2E)",             "Freewheeling"),
    25: ("E2E CNT fault",                 "Błąd licznika Alive Counter (zabezpieczenie E2E)",          "Freewheeling"),
    26: ("Invalid calibration",           "Nieprawidłowa kalibracja",                                  "Freewheeling"),
    27: ("E2E CRC fault (2)",             "Błąd sumy kontrolnej CRC (zabezpieczenie E2E)",             "Freewheeling"),
    28: ("E2E CNT fault (2)",             "Błąd licznika Alive Counter (zabezpieczenie E2E)",          "Freewheeling"),
    29: ("Crosscheck fault",              "Błąd weryfikacji krzyżowej",                                "Freewheeling"),
    30: ("Crosscheck torque",             "Błąd weryfikacji momentu obrotowego",                       "Freewheeling"),
    31: ("Wrong member",                  "Nieprawidłowy element konfiguracji",                        "Freewheeling"),
    32: ("Hardware supervisor fault",     "Błąd nadzoru sprzętowego (sprawdź zasilanie KL30/31)",      "Freewheeling"),
    33: ("KL30 undervoltage",             "Zbyt niskie napięcie zasilania LV (KL30)",                  "Freewheeling"),
    34: ("KL30 overvoltage",              "Zbyt wysokie napięcie zasilania LV (KL30)",                 "Freewheeling"),
    35: ("Lost message setpoint",         "Utracono ramkę sterującą (Timeout ramki setpoint)",         "Freewheeling"),
    36: ("Torque not coherent",           "Niespójność żądania momentu obrotowego",                    "Freewheeling"),
    37: ("LV External sensor supply flt", "Zwarcie zasilania 5V czujników zewn. lub błąd KL30/31",     "Freewheeling"),
    38: ("DCBus Overvoltage",             "Zbyt wysokie napięcie na szynie DC",                        "Freewheeling"),
    39: ("DCBus Setpoint not reached",    "Napięcie szyny DC różni się od zadanego (Setpoint)",        "Freewheeling")
}

FSM_STATES = {
    0:  "0: UNDEF / BRAK",
    1:  "1: INIT (Initialization state)",
    2:  "2: POST (Power-on self test)",
    3:  "3: STANDBY (Awaiting HV)",
    4:  "4: READY (HV detected, ready)",
    5:  "5: CURRENT (Current control - active)",
    6:  "6: TORQUE (Torque control - active)",
    7:  "7: SPEED (Speed control - active)",
    8:  "8: Placeholder",
    9:  "9: Placeholder",
    10: "10: FAULT_SOFT (Non-latched fault)",
    11: "11: FAULT_HARD (Latched fault)",
    12: "12: DISCHARGE (Discharge DC-Link)",
    13: "13: SHUTDOWN (Shut-down / power saving)",
    14: "14: OFF (LV is off)",
}

FSM_STATES_SHORT = {
    0:  "0:UNDEF",
    1:  "1:INIT",
    2:  "2:POST",
    3:  "3:STANDBY",
    4:  "4:READY",
    5:  "5:CURRENT",
    6:  "6:TORQUE",
    7:  "7:SPEED",
    8:  "8:Placeholder",
    9:  "9:Placeholder",
    10: "10:FAULT_SOFT",
    11: "11:FAULT_HARD",
    12: "12:DISCHARGE",
    13: "13:SHUTDOWN",
    14: "14:OFF",
}

CTRL_TYPE_MAP = {1: "Off", 2: "Scalar", 3: "Vectorial"}
CTRL_MODE_MAP = {1: "DuCyABC", 2: "DuCyDQZ", 3: "Current", 4: "Torque", 5: "Speed", 6: "Position"}
POS_SRC_MAP   = {1: "SimPos", 2: "Sensorless", 3: "AbsEnc", 4: "IncEnc", 5: "Res", 6: "xMR", 7: "BiSS"}

FIXED_CAN_IDS = [0x000, 0x360, 0x362, 0x460, 0x461, 0x462, 0x463, 0x464, 0x465, 0x466, 0x467, 0x468]
MAX_DEM_HISTORY_SLOTS = 5

# ==============================================================================
# 2. FUNKCJE POMOCNICZE I DEKODUJĄCE
# ==============================================================================

def decode_l1_pwrstg(bitfield_val: int) -> list:
    active = []
    for bit_val, (name, desc, safe_state) in L1_PWRSTG_BITS.items():
        if bitfield_val & bit_val:
            active.append({
                "bit_value": bit_val,
                "name": name,
                "description": desc,
                "safe_state": safe_state,
                "is_fault": bit_val >= 4
            })
    return active

def decode_l2_emctrl(bitfield_val: int) -> list:
    active = []
    for bit_val, (name, desc, safe_state) in L2_EMCTRL_BITS.items():
        if bitfield_val & bit_val:
            active.append({
                "bit_value": bit_val,
                "name": name,
                "description": desc,
                "safe_state": safe_state,
                "is_fault": bit_val >= 2
            })
    return active

def decode_l3_dem(dem_code: int) -> dict:
    if dem_code in L3_DEM_CODES:
        name, desc, safe_state = L3_DEM_CODES[dem_code]
    else:
        name, desc, safe_state = ("Nieznany kod DEM", f"Kod spoza tablicy ({dem_code})", "Unknown")
    return {
        "code": dem_code,
        "name": name,
        "description": desc,
        "safe_state": safe_state,
        "is_fault": dem_code != 0
    }

def u32_le(data: bytes, offset: int, default: int = 0) -> int:
    if offset + 4 <= len(data):
        return struct.unpack_from("<I", data, offset)[0]
    return default

def u16_le(data: bytes, offset: int, default: int = 0) -> int:
    if offset + 2 <= len(data):
        return struct.unpack_from("<H", data, offset)[0]
    return default

def s16_le(data: bytes, offset: int, default: int = 0) -> int:
    if offset + 2 <= len(data):
        return struct.unpack_from("<h", data, offset)[0]
    return default

def u8_le(data: bytes, offset: int, default: int = 0) -> int:
    if offset + 1 <= len(data):
        return data[offset]
    return default

# --- DEKODOWANIE I PAKOWANIE RAMKI 0x461 WG DBC (INTEL, LITTLE-ENDIAN, DLC=7) ---
def extract_461_signals(data: bytes) -> dict:
    """Dekoduje 7-bajtową ramkę 0x461 zgodnie ze strukturą bitową DBC."""
    raw = int.from_bytes(data, byteorder="little")
    return {
        "crc":           (raw >> 0)  & 0xFF,
        "cnt":           (raw >> 8)  & 0x0F,
        "zero":          (raw >> 12) & 0x0F,
        "dem_code":      (raw >> 16) & 0x7FFF,
        "dem_present":   (raw >> 31) & 0x01,
        "app_state_app": (raw >> 32) & 0x7F,
        "emctrl_bits":   (raw >> 39) & 0xFF,
        "pwrstg_bits":   (raw >> 47) & 0x1FF,
    }

def pack_461_signals(
    crc: int, cnt: int, dem_code: int, dem_pres: int,
    app_state: int, emctrl_bits: int, pwrstg_bits: int, zero: int = 0
) -> bytes:
    """Pakuje sygnały do 7 bajtów ramki 0x461 zgodnie z DBC."""
    raw = (
        ((crc & 0xFF)          << 0)  |
        ((cnt & 0x0F)          << 8)  |
        ((zero & 0x0F)         << 12) |
        ((dem_code & 0x7FFF)   << 16) |
        ((dem_pres & 0x01)     << 31) |
        ((app_state & 0x7F)    << 32) |
        ((emctrl_bits & 0xFF)  << 39) |
        ((pwrstg_bits & 0x1FF) << 47)
    )
    return raw.to_bytes(7, byteorder="little")

# --- DEKODOWANIE I PAKOWANIE RAMKI 0x463 WG DBC (INTEL, LITTLE-ENDIAN, DLC=8) ---
def extract_463_signals(data: bytes) -> dict:
    """
    Dekoduje 8-bajtową ramkę 0x463 (EMC_TX_STATE_4) zgodnie z DBC:
      - Current_D_A         : startbit 0,  len 16, Signed,   Factor 0.03125
      - Current_Q_A         : startbit 16, len 16, Signed,   Factor 0.03125
      - Volt_Modulus_permil : startbit 32, len 12, Unsigned, Factor 1
      - EMachine_Speed_erpm : startbit 44, len 20, Signed,   Factor 1
    """
    id_raw = s16_le(data, 0)
    iq_raw = s16_le(data, 2)
    b4_7 = u32_le(data, 4)

    v_mod = b4_7 & 0xFFF
    erpm_u20 = (b4_7 >> 12) & 0xFFFFF
    erpm = erpm_u20 - 0x100000 if (erpm_u20 & 0x80000) else erpm_u20

    return {
        "id_raw": id_raw,
        "iq_raw": iq_raw,
        "id_a": id_raw * 0.03125,
        "iq_a": iq_raw * 0.03125,
        "v_mod": v_mod,
        "erpm": erpm,
    }

def pack_463_signals(id_a: float, iq_a: float, v_mod: int, erpm: int) -> bytes:
    """Pakuje sygnały do 8 bajtów ramki 0x463 (EMC_TX_STATE_4) zgodnie z DBC."""
    id_raw = max(-32768, min(32767, int(round(id_a / 0.03125))))
    iq_raw = max(-32768, min(32767, int(round(iq_a / 0.03125))))
    v_mod_u12 = max(0, min(4095, int(v_mod))) & 0xFFF
    erpm_clamped = max(-524288, min(524287, int(erpm)))
    erpm_u20 = erpm_clamped & 0xFFFFF
    b4_7 = v_mod_u12 | (erpm_u20 << 12)
    return struct.pack("<hhI", id_raw, iq_raw, b4_7)

# --- DEKODOWANIE I PAKOWANIE RAMKI 0x464 WG DBC (INTEL, LITTLE-ENDIAN, DLC=4) ---
def extract_464_signals(data: bytes) -> dict:
    """
    Dekoduje 4-bajtową ramkę 0x464 (EMC_TX_STATE_5) zgodnie z DBC:
      - Board_Temp_degC      : startbit 0,  len 8, Unsigned, Factor 1, Offset -50, Zakres -49..200
      - PwrStg_Temp_degC     : startbit 8,  len 8, Unsigned, Factor 1, Offset -50, Zakres -49..200
      - EMachine_Temp_1_degC : startbit 16, len 8, Unsigned, Factor 1, Offset -50, Zakres -49..200
      - EMachine_Temp_2_degC : startbit 24, len 8, Unsigned, Factor 1, Offset -50, Zakres -49..200
    """
    raw_board  = u8_le(data, 0)
    raw_pwrstg = u8_le(data, 1)
    raw_em1    = u8_le(data, 2)
    raw_em2    = u8_le(data, 3)

    return {
        "raw_board":  raw_board,
        "raw_pwrstg": raw_pwrstg,
        "raw_em1":    raw_em1,
        "raw_em2":    raw_em2,
        "board_degc":  raw_board - 50,
        "pwrstg_degc": raw_pwrstg - 50,
        "em1_degc":    raw_em1 - 50,
        "em2_degc":    raw_em2 - 50,
    }

def format_temp_dbc(raw_val: int) -> str:
    """Formatuje temperaturę z uwzględnieniem zakresu DBC (-49..200 °C, 0xFF = brak czujnika/błąd)."""
    degc = raw_val - 50
    if raw_val == 0xFF or degc > 200 or degc < -49:
        return f"{degc}°C(N/A)"
    return f"{degc}°C"

# --- DEKODOWANIE I PAKOWANIE RAMKI 0x466 WG DBC (INTEL, LITTLE-ENDIAN, DLC=6) ---
def extract_466_signals(data: bytes) -> dict:
    """
    Dekoduje 6-bajtową ramkę 0x466 (EMC_TX_STATE_7) zgodnie ze strukturą bitową DBC:
      - E2E_P1_CRC      : startbit 0,  len 8,  Unsigned, Factor 1,      Offset 0
      - E2E_P1_CNT      : startbit 8,  len 4,  Unsigned, Factor 1,      Offset 0
      - E2E_P1_ZERO     : startbit 12, len 4,  Unsigned, Factor 1,      Offset 0
      - DCBus_Voltage_V : startbit 16, len 10, Unsigned, Factor 1,      Offset 0 (0..1023 V)
      - ACBus_Power_W   : startbit 26, len 16, Signed,   Factor 32.767, Offset 0
    """
    raw = int.from_bytes(data, byteorder="little")
    crc     = (raw >> 0)  & 0xFF
    cnt     = (raw >> 8)  & 0x0F
    zero    = (raw >> 12) & 0x0F
    v_dc    = (raw >> 16) & 0x3FF
    p_u16   = (raw >> 26) & 0xFFFF
    p_s16   = p_u16 - 0x10000 if (p_u16 & 0x8000) else p_u16
    p_ac_w  = p_s16 * 32.767

    return {
        "crc":       crc,
        "cnt":       cnt,
        "zero":      zero,
        "v_dc":      v_dc,
        "p_ac_raw":  p_s16,
        "p_ac_w":    p_ac_w,
    }

def pack_466_signals(crc: int, cnt: int, v_dc: int, p_ac_w: float, zero: int = 0) -> bytes:
    """Pakuje sygnały do 6 bajtów ramki 0x466 (EMC_TX_STATE_7) zgodnie z DBC."""
    v_dc_u10 = max(0, min(1023, int(v_dc))) & 0x3FF
    p_s16    = max(-32768, min(32767, int(round(p_ac_w / 32.767))))
    p_u16    = p_s16 & 0xFFFF

    raw = (
        ((crc & 0xFF)  << 0)  |
        ((cnt & 0x0F)  << 8)  |
        ((zero & 0x0F) << 12) |
        (v_dc_u10      << 16) |
        (p_u16         << 26)
    )
    return raw.to_bytes(6, byteorder="little")

# --- DEKODOWANIE I PAKOWANIE RAMKI 0x468 WG DBC (INTEL, LITTLE-ENDIAN, DLC=4) ---
def extract_468_signals(data: bytes) -> dict:
    """
    Dekoduje 4-bajtową ramkę 0x468 (EMC_TX_STATE_9) zgodnie ze strukturą bitową DBC:
      - E2E_P1_CRC    : startbit 0,  len 8,  Unsigned, Factor 1, Offset 0 (0..255)
      - E2E_P1_CNT    : startbit 8,  len 4,  Unsigned, Factor 1, Offset 0 (0..15)
      - E2E_P1_ZERO   : startbit 12, len 4,  Unsigned, Factor 1, Offset 0 (0..15)
      - Torque_Est_Nm : startbit 16, len 16, Signed,   Factor 1, Offset 0 (-32768..32767)
    """
    raw = int.from_bytes(data[:4], byteorder="little")
    crc     = (raw >> 0)  & 0xFF
    cnt     = (raw >> 8)  & 0x0F
    zero    = (raw >> 12) & 0x0F
    trq_u16 = (raw >> 16) & 0xFFFF
    trq_s16 = trq_u16 - 0x10000 if (trq_u16 & 0x8000) else trq_u16

    return {
        "crc":           crc,
        "cnt":           cnt,
        "zero":          zero,
        "torque_est_nm": trq_s16,
    }

def pack_468_signals(crc: int, cnt: int, torque_est_nm: int, zero: int = 0) -> bytes:
    """Pakuje sygnały do 4 bajtów ramki 0x468 (EMC_TX_STATE_9) zgodnie z DBC."""
    trq_s16 = max(-32768, min(32767, int(torque_est_nm)))
    trq_u16 = trq_s16 & 0xFFFF
    raw = (
        ((crc & 0xFF)  << 0)  |
        ((cnt & 0x0F)  << 8)  |
        ((zero & 0x0F) << 12) |
        (trq_u16       << 16)
    )
    return raw.to_bytes(4, byteorder="little")

def summarize_frame_inline(can_id: int, data: bytes) -> str:
    if can_id == 0x000:
        return "TX/RX: Ramka resetująca ID=0x000 (DLC=0)"
    elif can_id == 0x360 and len(data) >= 3:
        crc = data[0]
        cnt = data[1] & 0x0F
        zero = (data[1] >> 4) & 0x0F
        st = data[2] & 0x7F
        rst = (data[2] >> 7) & 0x01
        return f"TX/RX: App_State_Req={st} ({FSM_STATES_SHORT.get(st, '?')}), Flt_Clear={rst} (CRC=0x{crc:02X}, CNT={cnt}, ZERO={zero})"
    elif can_id == 0x362 and len(data) >= 4:
        crc = data[0]
        cnt = data[1] & 0x0F
        zero = (data[1] >> 4) & 0x0F
        trq = s16_le(data, 2)
        return f"TX/RX: Torque_Nm_Req = {trq} Nm (CRC=0x{crc:02X}, CNT={cnt}, ZERO={zero})"
    elif can_id == 0x460 and len(data) >= 6:
        uptime_ms = u32_le(data, 0)
        cpu0 = u8_le(data, 4)
        cpu1 = u8_le(data, 5)
        return f"Uptime={uptime_ms} ms ({uptime_ms/1000.0:.1f}s) | CPU0={cpu0}% | CPU1={cpu1}%"
    elif can_id == 0x461 and len(data) >= 7:
        s461 = extract_461_signals(data)
        dem_name = L3_DEM_CODES.get(s461["dem_code"], ("?", "", ""))[0]
        app_name = FSM_STATES_SHORT.get(s461["app_state_app"], f"{s461['app_state_app']}:?")
        return (f"App_State_App={app_name} | DEM_Code={s461['dem_code']} [{dem_name}] "
                f"(Pres={s461['dem_present']}) | L2_FOC=0x{s461['emctrl_bits']:02X} | L1_Pwr=0x{s461['pwrstg_bits']:03X}")
    elif can_id == 0x462 and len(data) >= 4:
        spd_raw = u16_le(data, 2)
        spd_s16 = spd_raw - 32767
        return f"EMachine_Speed_s16={spd_s16} (raw={spd_raw}, CRC=0x{data[0]:02X}, CNT={data[1]&0x0F})"
    elif can_id == 0x463 and len(data) >= 8:
        s463 = extract_463_signals(data)
        return (f"Speed={s463['erpm']} erpm | VoltMod={s463['v_mod']} permil | "
                f"Iq={s463['iq_a']:.2f} A | Id={s463['id_a']:.2f} A")
    elif can_id == 0x464 and len(data) >= 4:
        s464 = extract_464_signals(data)
        b_str  = format_temp_dbc(s464["raw_board"])
        ps_str = format_temp_dbc(s464["raw_pwrstg"])
        m1_str = format_temp_dbc(s464["raw_em1"])
        m2_str = format_temp_dbc(s464["raw_em2"])
        return f"Temp: Board={b_str} | PwrStg={ps_str} | EM1={m1_str} | EM2={m2_str}"
    elif can_id == 0x465 and len(data) >= 4:
        b0, b1 = data[0], data[1]
        cmd_src  = b0 & 0x0F
        ctrl_typ = (b0 >> 4) & 0x0F
        ctrl_mod = b1 & 0x0F
        pos_src  = (b1 >> 4) & 0x0F
        kl30_mv  = u16_le(data, 2)
        return (f"CmdSrc={cmd_src} | Type={CTRL_TYPE_MAP.get(ctrl_typ, str(ctrl_typ))} | "
                f"Mode={CTRL_MODE_MAP.get(ctrl_mod, str(ctrl_mod))} | "
                f"Pos={POS_SRC_MAP.get(pos_src, str(pos_src))} | KL30={kl30_mv} mV")
    elif can_id == 0x466 and len(data) >= 6:
        s466 = extract_466_signals(data)
        return (f"DCBus_Volt={s466['v_dc']} V | ACBus_Power={s466['p_ac_w']:.1f} W "
                f"(raw={s466['p_ac_raw']}, CRC=0x{s466['crc']:02X}, CNT={s466['cnt']})")
    elif can_id == 0x467 and len(data) >= 6:
        t_max = u16_le(data, 0) * 0.1
        id_a = s16_le(data, 2)
        iq_a = s16_le(data, 4)
        return f"TrqMaxFeas={t_max:.1f} Nm | Id_Req={id_a} A | Iq_Req={iq_a} A"
    elif can_id == 0x468 and len(data) >= 4:
        s468 = extract_468_signals(data)
        return (f"Torque_Est_Nm = {s468['torque_est_nm']} Nm "
                f"(CRC=0x{s468['crc']:02X}, CNT={s468['cnt']}, ZERO={s468['zero']})")
    else:
        words = [f"{s16_le(data, i)}" for i in range(0, len(data) - 1, 2)]
        return "Words(s16): " + ", ".join(words)

# ==============================================================================
# 3. KONTROLER NADAWANIA (TX) DO FALOWNIKA NX W90
# ==============================================================================

class InverterTxController:
    """
    Zarządza wysyłaniem ramek 0x360 (App_State_Req) oraz 0x362 (Torque_Nm_Req),
    sekwencją resetu (App_Req=13 -> ID=0 DLC=0 -> App_Req=3) oraz zerowaniem momentu
    przy wyjściu ze stanu 6 (TORQUE) przez użytkownika lub zmianę App_State_App.
    """
    def __init__(self, tx_hz: float = 50.0, use_strict_dbc_dlc: bool = True, offline_mode: bool = False):
        self.tx_hz = tx_hz
        self.tx_interval = 1.0 / max(1.0, tx_hz)
        self.use_strict_dbc_dlc = use_strict_dbc_dlc
        self.offline_mode = offline_mode

        self.app_state_req = 3
        self.flt_clear = 0
        self.torque_nm_req = 0
        self.actual_app_state = None
        self.inverter_was_in_torque = False
        self.sim_force_fault_state = False
        self.reset_stage = 0
        self.reset_stage_deadline = 0.0
        self.pending_id0_dlc0 = False

        self.tx_count = 0
        self.last_tx_error = ""
        self.last_user_action = "Gotowy: [A] Wpisz App_State_Req | [T] Wpisz Nm (w stanie 6) | [R] Sekwencja Resetu (13 -> ID0 -> 3)"
        self.input_mode = None
        self.input_buffer = ""

    def set_app_state_req(self, new_state: int, reason: str = "Użytkownik"):
        new_state = max(0, min(127, int(new_state)))
        old_state = self.app_state_req
        self.app_state_req = new_state

        if new_state != 6:
            if self.torque_nm_req != 0:
                self.torque_nm_req = 0
                self.last_user_action = (
                    f"[{reason}] Zmiana stanu {old_state} -> {new_state} ({FSM_STATES_SHORT.get(new_state, '?')}). "
                    f"WYJŚCIE ZE STANU TORQUE -> Moment wyzerowany do 0 Nm!"
                )
            else:
                self.last_user_action = f"[{reason}] Ustawiono App_State_Req = {FSM_STATES.get(new_state, str(new_state))} (Moment = 0 Nm)"
            if self.input_mode == "TORQUE":
                self.input_mode = None
                self.input_buffer = ""
        else:
            self.torque_nm_req = 0
            self.inverter_was_in_torque = False
            self.last_user_action = "Wchodzenie w stan 6: TORQUE (domyślnie 0 Nm). Naciśnij [T], aby wpisać docelowe Nm."

    def start_reset_sequence(self):
        self.torque_nm_req = 0
        self.input_mode = None
        self.input_buffer = ""
        self.app_state_req = 13
        self.reset_stage = 1
        self.reset_stage_deadline = time.time() + 0.15
        self.last_user_action = "SEKWENCJA RESETU [Krok 1/3]: Wysyłanie App_State_Req = 13 (SHUTDOWN)..."

    def update_reset_sequence(self, now: float):
        if self.reset_stage == 1 and now >= self.reset_stage_deadline:
            self.pending_id0_dlc0 = True
            self.reset_stage = 2
            self.reset_stage_deadline = now + 0.05
            self.last_user_action = "SEKWENCJA RESETU [Krok 2/3]: Wysłano ramkę ID=0x000 (DLC=0)!"
        elif self.reset_stage == 2 and now >= self.reset_stage_deadline:
            self.app_state_req = 3
            self.torque_nm_req = 0
            self.reset_stage = 0
            self.sim_force_fault_state = False
            self.last_user_action = "SEKWENCJA RESETU [Krok 3/3]: Zakończono -> Ustawiono App_State_Req = 3 (STANDBY), Torque = 0 Nm."

    def on_rx_461(self, s461: dict):
        app_st = s461["app_state_app"]
        self.actual_app_state = app_st

        if app_st == 6:
            self.inverter_was_in_torque = True
        else:
            if self.inverter_was_in_torque:
                self.inverter_was_in_torque = False
                if self.torque_nm_req != 0 or self.input_mode == "TORQUE":
                    self.torque_nm_req = 0
                    if self.input_mode == "TORQUE":
                        self.input_mode = None
                        self.input_buffer = ""
                    self.last_user_action = (
                        f"[AUTO-ZERO] Zmiana App_State_App z 6 (TORQUE) na {app_st} "
                        f"({FSM_STATES_SHORT.get(app_st, '?')})! Moment wyzerowany do 0 Nm!"
                    )
            elif self.torque_nm_req != 0:
                self.torque_nm_req = 0
                if self.input_mode == "TORQUE":
                    self.input_mode = None
                    self.input_buffer = ""
                self.last_user_action = (
                    f"[AUTO-ZERO] Aktualny App_State_App={app_st} ({FSM_STATES_SHORT.get(app_st, '?')}) != 6 (TORQUE). "
                    f"Moment wyzerowany do 0 Nm!"
                )

    def build_frames(self):
        """
        Buduje ramki 0x360 (DLC=3) oraz 0x362 (DLC=4) zgodnie ze specyfikacją DBC:
          - 0x360 (EMC_RX_SETPOINT_1, 3 bajty):
              * E2E_P1_CRC    : startbit 0,  len 8 (Unsigned)
              * E2E_P1_CNT    : startbit 8,  len 4 (Unsigned)
              * E2E_P1_ZERO   : startbit 12, len 4 (Unsigned)
              * App_State_Req : startbit 16, len 7 (Unsigned)
              * Flt_Clear     : startbit 23, len 1 (Unsigned)
          - 0x362 (EMC_RX_SETPOINT_3, 4 bajty):
              * E2E_P1_CRC    : startbit 0,  len 8 (Unsigned)
              * E2E_P1_CNT    : startbit 8,  len 4 (Unsigned)
              * E2E_P1_ZERO   : startbit 12, len 4 (Unsigned)
              * Torque_Nm_Req : startbit 16, len 16 (Signed, -32768..32767)
        """
        if self.app_state_req != 6:
            self.torque_nm_req = 0
        elif self.actual_app_state is not None and self.actual_app_state != 6:
            self.torque_nm_req = 0

        byte2_360 = (self.app_state_req & 0x7F) | ((self.flt_clear & 0x01) << 7)
        raw_360 = struct.pack("<BBB", 0x00, 0x00, byte2_360)

        torque_clamped = max(-32768, min(32767, int(self.torque_nm_req)))
        raw_362 = struct.pack("<BBh", 0x00, 0x00, torque_clamped)

        if not self.use_strict_dbc_dlc:
            raw_360 = raw_360 + bytes(5)
            raw_362 = raw_362 + bytes(4)

        return raw_360, raw_362

    def handle_key(self, key: str) -> bool:
        if self.input_mode is not None:
            if key in ("\r", "\n"):
                buf = self.input_buffer.strip()
                mode = self.input_mode
                self.input_mode = None
                self.input_buffer = ""
                if not buf:
                    self.last_user_action = "Anulowano wpisywanie (pusta wartość)."
                    return True
                try:
                    val = int(buf)
                    if mode == "STATE":
                        self.set_app_state_req(val, reason="Wpisano ręcznie")
                    elif mode == "TORQUE":
                        if self.app_state_req != 6:
                            self.torque_nm_req = 0
                            self.last_user_action = "ODRZUCONO: Zadawanie Nm dozwolone tylko w stanie 6 (TORQUE)! Wysyłane 0 Nm."
                        elif self.actual_app_state is not None and self.actual_app_state != 6:
                            self.torque_nm_req = 0
                            self.last_user_action = (
                                f"ODRZUCONO: Falownik nie jest w stanie 6 (aktualny App_State_App={self.actual_app_state})! "
                                f"Wysyłane 0 Nm."
                            )
                        else:
                            self.torque_nm_req = max(-2000, min(2000, val))
                            self.last_user_action = f"Zatwierdzono i wysyłany nowy moment obrotowy: {self.torque_nm_req} Nm"
                except ValueError:
                    self.last_user_action = "Błąd: Wprowadzono nieprawidłową liczbę całkowitą!"
            elif key == "\x1b":
                self.input_mode = None
                self.input_buffer = ""
                self.last_user_action = "Anulowano wpisywanie (ESC)."
            elif key in ("\x08", "\x7f", "BACKSPACE"):
                self.input_buffer = self.input_buffer[:-1]
            elif key in "-0123456789" and len(self.input_buffer) < 8:
                if key == "-" and (len(self.input_buffer) > 0 or self.input_mode == "STATE"):
                    return True
                self.input_buffer += key
            return True

        k_up = key.upper()
        if k_up == "Q":
            return False
        elif k_up == "R":
            self.start_reset_sequence()
        elif k_up == "A":
            self.input_mode = "STATE"
            self.input_buffer = ""
            self.last_user_action = "Wpisz docelowy stan App_State_Req (np. 3, 4, 6, 12, 13) i zatwierdź ENTER..."
        elif k_up == "T":
            if self.app_state_req != 6:
                self.torque_nm_req = 0
                self.last_user_action = "BLOKADA: Najpierw ustaw stan App_State_Req = 6 (TORQUE), aby móc wpisać Nm!"
            elif self.actual_app_state is not None and self.actual_app_state != 6:
                self.torque_nm_req = 0
                self.last_user_action = (
                    f"BLOKADA: Falownik jest w stanie {self.actual_app_state} "
                    f"({FSM_STATES_SHORT.get(self.actual_app_state, '?')}), a nie 6 (TORQUE)!"
                )
            else:
                self.input_mode = "TORQUE"
                self.input_buffer = ""
                self.last_user_action = "Wpisz moment obrotowy w Nm (np. 20 lub -15) i zatwierdź ENTER..."
        elif k_up == "F" and self.offline_mode:
            self.sim_force_fault_state = not self.sim_force_fault_state
            if self.sim_force_fault_state:
                self.last_user_action = "[SYMULACJA] Wymuszono zmianę App_State_App na 11 (FAULT_HARD)!"
            else:
                self.last_user_action = "[SYMULACJA] Wyłączono wymuszenie FAULT_HARD -> App_State_App podąża za App_State_Req."
        elif key == " ":
            self.torque_nm_req = 0
            self.last_user_action = "Wyzerowano moment obrotowy (Torque_Nm_Req = 0 Nm)."
        elif key in ("1", "2", "3", "4", "5", "6", "7"):
            self.set_app_state_req(int(key), reason=f"Skrót [{key}]")
        elif key == "8":
            self.set_app_state_req(12, reason="Skrót [8]")
        elif key == "9":
            self.set_app_state_req(13, reason="Skrót [9]")
        elif key == "0":
            self.set_app_state_req(14, reason="Skrót [0]")

        return True

# ==============================================================================
# 4. NIEBLOKUJĄCY ODCZYT KLAWIATURY
# ==============================================================================

class NonBlockingKeyboard:
    def __enter__(self):
        self.os_nt = (os.name == "nt")
        self.old_settings = None
        if not self.os_nt and sys.stdin.isatty():
            try:
                self.fd = sys.stdin.fileno()
                self.old_settings = termios.tcgetattr(self.fd)
                tty.setcbreak(self.fd)
            except Exception:
                self.old_settings = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.os_nt and self.old_settings is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)
            except Exception:
                pass

    def poll_key(self):
        if self.os_nt:
            if not msvcrt.kbhit():
                return None
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                ch2 = msvcrt.getwch()
                arrow_map = {"H": "UP", "P": "DOWN", "M": "RIGHT", "K": "LEFT"}
                return arrow_map.get(ch2, None)
            return ch
        else:
            if not sys.stdin.isatty():
                return None
            dr, _, _ = select.select([sys.stdin], [], [], 0.0)
            if not dr:
                return None
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                dr2, _, _ = select.select([sys.stdin], [], [], 0.005)
                if dr2:
                    ch2 = sys.stdin.read(1)
                    if ch2 == "[":
                        dr3, _, _ = select.select([sys.stdin], [], [], 0.005)
                        if dr3:
                            ch3 = sys.stdin.read(1)
                            arrow_map = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}
                            return arrow_map.get(ch3, "\x1b")
                return "\x1b"
            return ch

# ==============================================================================
# 5. KLASA TABLICY KONSOLOWEJ
# ==============================================================================

class CanConsoleDashboard:
    def __init__(self, mode_title: str = "LIVE CAN", tx_ctrl: InverterTxController = None):
        self.mode_title = mode_title
        self.tx_ctrl = tx_ctrl or InverterTxController()
        self.start_time = time.time()
        self.total_rx = 0
        self.frames = {}
        self.seen_dem_461 = {}

    def update_frame(self, can_id: int, raw_bytes: bytes):
        now = time.time()
        if can_id >= 0x460:
            self.total_rx += 1
        if can_id in self.frames:
            prev = self.frames[can_id]
            dt_ms = (now - prev["last_ts"]) * 1000.0
            if dt_ms > 0.2:
                period_ms = 0.8 * prev["period_ms"] + 0.2 * dt_ms if prev["period_ms"] > 0 else dt_ms
            else:
                period_ms = prev["period_ms"]
            count = prev["count"] + 1
        else:
            period_ms = 0.0
            count = 1

        self.frames[can_id] = {
            "data": raw_bytes,
            "count": count,
            "last_ts": now,
            "period_ms": period_ms
        }

        if can_id == 0x461 and len(raw_bytes) >= 7:
            s461 = extract_461_signals(raw_bytes)
            self.tx_ctrl.on_rx_461(s461)
            c461 = s461["dem_code"]
            if c461 in L3_DEM_CODES and c461 != 0:
                self.seen_dem_461[c461] = now

    def render_to_string(self) -> str:
        term_cols = shutil.get_terminal_size((120, 44)).columns - 1
        lines = []
        now = time.time()
        uptime = now - self.start_time
        tx = self.tx_ctrl

        def add_line(text: str = ""):
            lines.append(fit_line_to_terminal(text, term_cols))

        add_line(f"{C_BOLD}{C_CYAN}{'=' * 114}{C_RESET}")
        add_line(f"{C_BOLD} NX W90 - STEROWANIE I RESET CAN (v15) [{self.mode_title}] | Czas: {uptime:5.1f}s | RX:{self.total_rx:<7d} TX:{tx.tx_count:<7d}{C_RESET}")
        add_line(f"{C_BOLD}{C_CYAN}{'=' * 114}{C_RESET}")

        raw_360, raw_362 = tx.build_frames()
        hex_360 = " ".join(f"{b:02X}" for b in raw_360)
        hex_362 = " ".join(f"{b:02X}" for b in raw_362)
        st_req_name = FSM_STATES.get(tx.app_state_req, f"{tx.app_state_req}: CUSTOM")

        if tx.reset_stage == 1:
            rst_badge = f"{C_YELLOW}{C_BOLD}KROK 1/3: App_Req=13 (SHUTDOWN){C_RESET}"
        elif tx.reset_stage == 2:
            rst_badge = f"{C_MAGENTA}{C_BOLD}KROK 2/3: Wysłano ID=0x000 (DLC=0) -> przejście na 3{C_RESET}"
        else:
            rst_badge = f"{C_GREEN}Gotowy (Naciśnij [R]: 13 -> ID0(DLC0) -> 3){C_RESET}"

        if 0x461 in self.frames and len(self.frames[0x461]["data"]) >= 7:
            s461_top = extract_461_signals(self.frames[0x461]["data"])
            app_st_val = s461_top["app_state_app"]
            app_st_name = FSM_STATES.get(app_st_val, f"{app_st_val}: NIEZNANY")
            if app_st_val == tx.app_state_req:
                app_col = C_GREEN
                sync_txt = "[ZGODNY Z REQ]"
            elif app_st_val in (10, 11):
                app_col = C_RED
                sync_txt = "[STAN BŁĘDU FSM -> MOMENT WYZEROWANY!]"
            else:
                app_col = C_YELLOW
                sync_txt = "[PRZEJŚCIE / RÓŻNY OD REQ]"
            app_state_display = f"{app_col}{C_BOLD}{app_st_name:<35s}{C_RESET} {app_col}{sync_txt}{C_RESET}"
        else:
            app_state_display = f"{C_GRAY}Oczekiwanie na ramkę 0x461...{C_RESET}"

        trq_col = C_GREEN if tx.torque_nm_req == 0 else C_YELLOW
        trq_rule_info = (
            f"{C_GREEN}[AKTYWNY STAN 6: Możesz zadać Nm klawiszem [T]]{C_RESET}"
            if (tx.app_state_req == 6 and (tx.actual_app_state in (None, 6)))
            else f"{C_GRAY}[Auto-Zero po wyjściu ze stanu 6]{C_RESET}"
        )
        sim_hint = f" | {C_YELLOW}[F]{C_RESET} Symuluj zmianę App_State na 11" if tx.offline_mode else ""

        add_line(f"{C_BOLD}{C_MAGENTA} PANEL STEROWANIA I STANU FSM{C_RESET} | Cykliczne TX: {C_GREEN}50 Hz (DLC: 3B/4B){C_RESET} | Reset: {rst_badge}")
        add_line(f"  • {C_BOLD}0x360 (Żądany Req)  {C_RESET}: App_State_Req = {C_YELLOW}{C_BOLD}{st_req_name:<35s}{C_RESET} | TX: [{C_CYAN}{hex_360}{C_RESET}]")
        add_line(f"  • {C_BOLD}0x461 (Aktualny App){C_RESET}: App_State_App = {app_state_display}")
        add_line(f"  • {C_BOLD}0x362 (Moment Nm)   {C_RESET}: Torque_Nm_Req = {trq_col}{C_BOLD}{tx.torque_nm_req:5d} Nm{C_RESET}  {trq_rule_info:<48s} | TX: [{C_CYAN}{hex_362}{C_RESET}]")
        add_line(f" {C_WHITE}Klawisze:{C_RESET} {C_YELLOW}[A]{C_RESET} Wpisz stan App_Req (1-14) | {C_YELLOW}[T]{C_RESET} Wpisz moment Nm (w stanie 6) | {C_YELLOW}[R]{C_RESET} Reset (13->ID0->3) | {C_YELLOW}[Spacja]{C_RESET} 0 Nm{sim_hint} | {C_YELLOW}[Q]{C_RESET} Wyjście")
        add_line(f" {C_GRAY}Szybkie stany: [1]Init [2]Post [3]Standby [4]Ready [5]Curr [6]Torque [7]Speed [8]Disch(12) [9]Shutdn(13) [0]Off(14){C_RESET}")

        if tx.input_mode == "TORQUE":
            add_line(f" {C_YELLOW}{C_BOLD}>>> WPISZ MOMENT OBROTOWY [Nm] I NACIŚNIJ ENTER (ESC=Anuluj): {tx.input_buffer}_{C_RESET}")
        elif tx.input_mode == "STATE":
            add_line(f" {C_YELLOW}{C_BOLD}>>> WPISZ STAN APP_STATE_REQ [1-14] (np. 3=STANDBY, 4=READY, 6=TORQUE, 13=SHUTDOWN) + ENTER: {tx.input_buffer}_{C_RESET}")
        elif tx.last_tx_error:
            add_line(f" {C_RED}[BŁĄD TX CAN]: {tx.last_tx_error}{C_RESET}")
        else:
            add_line(f" {C_CYAN}[Status / Akcja]: {tx.last_user_action}{C_RESET}")

        add_line("=" * 114)
        add_line(f"{C_BOLD} ID HEX (DEC) | DLC | LICZNIK | CYKL ms | SUROWE BAJTY (HEX)      | ZDEKODOWANE SYGNAŁY WG TABELI CAN DATABASE{C_RESET}")
        add_line("-" * 114)

        for cid in FIXED_CAN_IDS:
            if cid in self.frames:
                info = self.frames[cid]
                data = info["data"]
                dlc = len(data)
                cnt = info["count"]
                per = info["period_ms"]
                hex_str = " ".join(f"{b:02X}" for b in data) if dlc > 0 else "(DLC=0 Pusta)"
                summary = summarize_frame_inline(cid, data)
                age = now - info["last_ts"]
                if cid in (0x000, 0x360, 0x362):
                    row_color = C_CYAN if age < 1.5 else C_GRAY
                else:
                    row_color = C_WHITE if age < 1.0 else C_GRAY
                add_line(f"{row_color} 0x{cid:03X} ({cid:4d}) |  {dlc}  | {cnt:7d} | {per:6.1f}  | {hex_str:<23s} | {summary}{C_RESET}")
            else:
                if cid == 0x000:
                    add_line(f"{C_GRAY} 0x000 (   0) |  0  |       0 |    0.0  | -- (Tylko podczas [R])  | Ramka resetu ID=0 (DLC=0) wysyłana w sekwencji [R]{C_RESET}")
                elif cid in (0x360, 0x362):
                    add_line(f"{C_GRAY} 0x{cid:03X} ({cid:4d}) |  -  |       0 |    0.0  | --                      | Oczekiwanie na wysłanie...{C_RESET}")
                else:
                    add_line(f"{C_GRAY} 0x{cid:03X} ({cid:4d}) |  -  |       0 |    0.0  | --                      | Oczekiwanie na telemetrię z falownika...{C_RESET}")

        add_line("=" * 114)
        add_line(f"{C_BOLD}{C_YELLOW} DIAGNOSTYKA FALOWNIKA NA ŻYWO Z RAMKI 0x461 (EMC_TX_STATE_2: L1 PowerStage | L2 EMCtrl | L3 DEM):{C_RESET}")
        add_line("-" * 114)

        if 0x461 in self.frames and len(self.frames[0x461]["data"]) >= 7:
            s461 = extract_461_signals(self.frames[0x461]["data"])
            l1_val = s461["pwrstg_bits"]
            l2_val = s461["emctrl_bits"]
            dem_code = s461["dem_code"]
            dem_pres = s461["dem_present"]
            app_st_val = s461["app_state_app"]

            l1_list = decode_l1_pwrstg(l1_val)
            l1_faults = [x for x in l1_list if x["is_fault"]]
            l1_status = [x for x in l1_list if not x["is_fault"]]
            status_str_l1 = ", ".join(f"{x['name']}(Bit {x['bit_value']})" for x in l1_status) or "Brak (Stopień mocy wyłączony)"

            add_line(f" {C_BOLD}[L1 PwrStg_BitState]{C_RESET} Wartość={l1_val:3d} (0x{l1_val:03X}) | Status: {status_str_l1}")
            if l1_faults:
                flt_str_l1 = ", ".join(f"{x['name']} ({x['description']} -> {x['safe_state']})" for x in l1_faults)
                add_line(f"                      {C_RED}{C_BOLD}[BŁĄD L1!]: {flt_str_l1}{C_RESET}")
            else:
                add_line(f"                      {C_GREEN}[OK] Brak aktywnych błędów sprzętowych L1.{C_RESET}")

            l2_list = decode_l2_emctrl(l2_val)
            l2_faults = [x for x in l2_list if x["is_fault"]]
            l2_status = [x for x in l2_list if not x["is_fault"]]
            status_str_l2 = ", ".join(f"{x['name']}(Bit {x['bit_value']})" for x in l2_status) or "Brak"

            add_line(f" {C_BOLD}[L2 EMCtrl_FOC_Bit]{C_RESET}  Wartość={l2_val:3d} (0x{l2_val:02X})  | Status: {status_str_l2}")
            if l2_faults:
                flt_str_l2 = ", ".join(f"Bit {x['bit_value']} {x['name']} ({x['description']} -> {x['safe_state']})" for x in l2_faults)
                add_line(f"                      {C_RED}{C_BOLD}[BŁĄD L2!]: {flt_str_l2}{C_RESET}")
            else:
                add_line(f"                      {C_GREEN}[OK] Brak aktywnych błędów sterowania FOC L2.{C_RESET}")

            add_line("-" * 114)
            d461_info = decode_l3_dem(dem_code)
            col_dem = C_RED if (d461_info["is_fault"] or dem_pres) else C_GREEN
            app_st_str = FSM_STATES.get(app_st_val, f"Nieznany ({app_st_val})")
            add_line(f" {C_BOLD}[0x461 B4 App_State]{C_RESET} Aktualny stan falownika: {C_YELLOW}{C_BOLD}{app_st_str}{C_RESET} (Żądany w 0x360: {C_CYAN}{FSM_STATES_SHORT.get(tx.app_state_req, '?')}{C_RESET})")
            add_line(f" {C_BOLD}[0x461 L3 DEM]      {C_RESET}DEM_Code={col_dem}{C_BOLD}{dem_code:2d} -> {d461_info['name']}{C_RESET} ({d461_info['description']}) | DEM_Present={dem_pres}")
        else:
            add_line(f" {C_GRAY}[L1 PwrStg_BitState] Oczekiwanie na pełną (7-bajtową) ramkę 0x461...{C_RESET}")
            add_line(f" {C_GRAY}                     --{C_RESET}")
            add_line(f" {C_GRAY}[L2 EMCtrl_FOC_Bit]  Oczekiwanie na pełną (7-bajtową) ramkę 0x461...{C_RESET}")
            add_line(f" {C_GRAY}                     --{C_RESET}")
            add_line("-" * 114)
            add_line(f" {C_GRAY}[0x461 B4 App_State] Oczekiwanie na pełną (7-bajtową) ramkę 0x461...{C_RESET}")
            add_line(f" {C_GRAY}[0x461 L3 DEM]       Oczekiwanie na pełną (7-bajtową) ramkę 0x461...{C_RESET}")

        return "\n".join(lines) + CLEAR_BELOW

    def refresh_screen(self):
        sys.stdout.write(CURSOR_HOME + self.render_to_string())
        sys.stdout.flush()

# ==============================================================================
# 6. TRYB LIVE (PCAN-USB) ORAZ TRYB OFFLINE (PODGLĄD BEZ PCAN-USB)
# ==============================================================================

def listen_pcan_live_dashboard(interface="pcan", channel="PCAN_USBBUS1", bitrate=500000, refresh_hz=10.0, tx_hz=50.0):
    try:
        import can
    except ImportError:
        print(f"\n{C_RED}[BŁĄD] Biblioteka 'python-can' nie jest zainstalowana!{C_RESET}")
        print("Zainstaluj ją poleceniem: pip install python-can")
        return

    print(f"\n[LIVE] Łączenie z interfejsem {interface} ({channel}) @ {bitrate} bps...")
    try:
        bus = can.interface.Bus(interface=interface, channel=channel, bitrate=bitrate)
    except Exception as e:
        print(f"\n{C_RED}[BŁĄD CAN] Nie udało się otworzyć interfejsu CAN: {e}{C_RESET}")
        return

    tx_ctrl = InverterTxController(tx_hz=tx_hz, use_strict_dbc_dlc=True, offline_mode=False)
    dash = CanConsoleDashboard(mode_title=f"LIVE {interface.upper()} {channel} @ {bitrate//1000}kbps", tx_ctrl=tx_ctrl)

    refresh_interval = 1.0 / max(1.0, refresh_hz)
    next_draw = time.time()
    next_tx = time.time()

    sys.stdout.write(CURSOR_HIDE + CLEAR_SCREEN)
    sys.stdout.flush()

    try:
        with NonBlockingKeyboard() as kb:
            while True:
                while True:
                    key = kb.poll_key()
                    if key is None:
                        break
                    if not tx_ctrl.handle_key(key):
                        raise KeyboardInterrupt

                now = time.time()
                tx_ctrl.update_reset_sequence(now)

                if tx_ctrl.pending_id0_dlc0:
                    tx_ctrl.pending_id0_dlc0 = False
                    try:
                        msg_id0 = can.Message(arbitration_id=0x000, data=b"", dlc=0, is_extended_id=False)
                        bus.send(msg_id0)
                        tx_ctrl.tx_count += 1
                        dash.update_frame(0x000, b"")
                    except Exception as e:
                        tx_ctrl.last_tx_error = f"Błąd wysyłania ID=0 (DLC=0): {e}"

                if now >= next_tx:
                    raw_360, raw_362 = tx_ctrl.build_frames()
                    try:
                        msg_360 = can.Message(arbitration_id=0x360, data=raw_360, is_extended_id=False)
                        msg_362 = can.Message(arbitration_id=0x362, data=raw_362, is_extended_id=False)
                        bus.send(msg_360)
                        bus.send(msg_362)
                        tx_ctrl.tx_count += 2
                        tx_ctrl.last_tx_error = ""
                        dash.update_frame(0x360, raw_360)
                        dash.update_frame(0x362, raw_362)
                    except Exception as e:
                        tx_ctrl.last_tx_error = str(e)
                    next_tx = now + tx_ctrl.tx_interval

                msg = bus.recv(timeout=0.005)
                if msg is not None:
                    if (0x460 <= msg.arbitration_id <= 0x468) or (0x360 <= msg.arbitration_id <= 0x364):
                        dash.update_frame(msg.arbitration_id, bytes(msg.data))

                    for _ in range(200):
                        m_next = bus.recv(timeout=0.0)
                        if m_next is None:
                            break
                        if (0x460 <= m_next.arbitration_id <= 0x468) or (0x360 <= m_next.arbitration_id <= 0x364):
                            dash.update_frame(m_next.arbitration_id, bytes(m_next.data))

                now = time.time()
                if now >= next_draw:
                    dash.refresh_screen()
                    next_draw = now + refresh_interval

    except KeyboardInterrupt:
        try:
            safe_360 = struct.pack("<BBB", 0x00, 0x00, 0x03)
            safe_362 = struct.pack("<BBh", 0x00, 0x00, 0)
            bus.send(can.Message(arbitration_id=0x360, data=safe_360, is_extended_id=False))
            bus.send(can.Message(arbitration_id=0x362, data=safe_362, is_extended_id=False))
        except Exception:
            pass
        sys.stdout.write(CURSOR_SHOW)
        sys.stdout.flush()
        print(f"\n\n{C_YELLOW}[LIVE] Zatrzymano program i wysłano bezpieczną ramkę (0 Nm, STANDBY=3).{C_RESET}")
    finally:
        sys.stdout.write(CURSOR_SHOW)
        sys.stdout.flush()
        try:
            bus.shutdown()
        except Exception:
            pass

def run_offline_dashboard(refresh_hz=10.0, tx_hz=50.0, duration_sec=0.0):
    tx_ctrl = InverterTxController(tx_hz=tx_hz, use_strict_dbc_dlc=True, offline_mode=True)
    dash = CanConsoleDashboard(mode_title="TRYB OFFLINE (PODGLĄD BEZ PCAN-USB)", tx_ctrl=tx_ctrl)

    refresh_interval = 1.0 / max(1.0, refresh_hz)
    next_draw = time.time()
    next_tx = time.time()
    next_sim_rx = time.time()
    t0 = time.time()

    sys.stdout.write(CURSOR_HIDE + CLEAR_SCREEN)
    sys.stdout.flush()

    try:
        with NonBlockingKeyboard() as kb:
            while True:
                while True:
                    key = kb.poll_key()
                    if key is None:
                        break
                    if not tx_ctrl.handle_key(key):
                        raise KeyboardInterrupt

                now = time.time()
                tx_ctrl.update_reset_sequence(now)

                if tx_ctrl.pending_id0_dlc0:
                    tx_ctrl.pending_id0_dlc0 = False
                    tx_ctrl.tx_count += 1
                    dash.update_frame(0x000, b"")

                if now >= next_tx:
                    raw_360, raw_362 = tx_ctrl.build_frames()
                    tx_ctrl.tx_count += 2
                    dash.update_frame(0x360, raw_360)
                    dash.update_frame(0x362, raw_362)
                    next_tx = now + tx_ctrl.tx_interval

                if now >= next_sim_rx:
                    uptime_ms = int((now - t0) * 1000)
                    sim_app_state = 11 if tx_ctrl.sim_force_fault_state else tx_ctrl.app_state_req
                    sim_dem_code = 5 if tx_ctrl.sim_force_fault_state else 21
                    sim_l2_bits = 129 if tx_ctrl.sim_force_fault_state else 1
                    sim_l1_bits = 3 if sim_app_state == 6 else 1

                    f460 = struct.pack("<IBB", uptime_ms, 18, 24)
                    f461 = pack_461_signals(
                        crc=0x6C,
                        cnt=0x01,
                        dem_code=sim_dem_code,
                        dem_pres=1,
                        app_state=sim_app_state,
                        emctrl_bits=sim_l2_bits,
                        pwrstg_bits=sim_l1_bits
                    )
                    f462 = struct.pack("<BBH", 0x52, 0x01, 32767)
                    f463 = pack_463_signals(
                        id_a=-0.15625 if tx_ctrl.torque_nm_req == 0 else 0.0,
                        iq_a=0.25 if tx_ctrl.torque_nm_req == 0 else float(tx_ctrl.torque_nm_req * 2.0),
                        v_mod=0 if tx_ctrl.torque_nm_req == 0 else 250,
                        erpm=0
                    )
                    f464 = struct.pack("<BBBB", 52 + 50, 37 + 50, 0xFF, 0xFF)
                    f465 = struct.pack("<BBH", 0x32, 0x54, 13250)
                    f466 = pack_466_signals(crc=0x4A, cnt=0x01, v_dc=380, p_ac_w=float(tx_ctrl.torque_nm_req * 100.0))
                    f467 = struct.pack("<Hhh", 2400, 0, int(tx_ctrl.torque_nm_req * 2))
                    f468 = pack_468_signals(crc=0x7E, cnt=0x01, torque_est_nm=int(tx_ctrl.torque_nm_req), zero=0)

                    for cid, payload in (
                        (0x460, f460), (0x461, f461), (0x462, f462),
                        (0x463, f463), (0x464, f464), (0x465, f465),
                        (0x466, f466), (0x467, f467), (0x468, f468)
                    ):
                        dash.update_frame(cid, payload)

                    next_sim_rx = now + 0.02

                if now >= next_draw:
                    dash.refresh_screen()
                    next_draw = now + refresh_interval

                if duration_sec > 0 and (now - t0) >= duration_sec:
                    break
                time.sleep(0.005)
    except KeyboardInterrupt:
        sys.stdout.write(CURSOR_SHOW)
        sys.stdout.flush()
        print(f"\n\n{C_YELLOW}[OFFLINE] Zakończono podgląd offline.{C_RESET}")
    finally:
        sys.stdout.write(CURSOR_SHOW)
        sys.stdout.flush()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sterowanie CAN i Reset dla falownika NX W90 (v15)")
    parser.add_argument("--live", action="store_true", help="Od razu uruchom tryb LIVE z PCAN-USB")
    parser.add_argument("--offline", "--sim", dest="offline", action="store_true", help="Od razu uruchom podgląd OFFLINE bez PCAN-USB")
    parser.add_argument("--channel", type=str, default="PCAN_USBBUS1", help="Kanał PCAN (domyślnie PCAN_USBBUS1)")
    parser.add_argument("--bitrate", type=int, default=500000, help="Bitrate CAN w bps (domyślnie 500000)")
    parser.add_argument("--hz", type=float, default=10.0, help="Odświeżanie ekranu w Hz (domyślnie 10.0)")
    parser.add_argument("--tx-hz", type=float, default=50.0, help="Częstotliwość wysyłania 0x360/0x362 w Hz (domyślnie 50.0)")
    args = parser.parse_args()

    if args.offline:
        run_offline_dashboard(refresh_hz=args.hz, tx_hz=args.tx_hz)
    elif args.live:
        listen_pcan_live_dashboard(interface="pcan", channel=args.channel, bitrate=args.bitrate, refresh_hz=args.hz, tx_hz=args.tx_hz)
    else:
        print("=" * 78)
        print(" NX TECHNOLOGIES W90 - KONSOLA STEROWANIA CAN (v15)")
        print("=" * 78)
        print("  [1] Tryb LIVE (PEAK PCAN-USB)")
        print("  [2] Tryb OFFLINE (Podgląd i testowanie sterowania bez podpiętego PCAN)")
        print("  [0] Wyjście")
        print("-" * 78)
        mode_choice = input("Wybierz tryb [1/2, Enter = 1]: ").strip() or "1"

        if mode_choice == "2":
            run_offline_dashboard(refresh_hz=args.hz, tx_hz=args.tx_hz)
        elif mode_choice == "1":
            ch_in = input(f"Podaj kanał PCAN [Enter = {args.channel}]: ").strip() or args.channel
            br_in = input(f"Podaj bitrate [Enter = {args.bitrate}]: ").strip() or str(args.bitrate)
            listen_pcan_live_dashboard(interface="pcan", channel=ch_in, bitrate=int(br_in), refresh_hz=args.hz, tx_hz=args.tx_hz)
