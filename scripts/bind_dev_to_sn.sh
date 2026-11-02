#!/usr/bin/env bash
# Серийники Stereolabs GMSL на Jetson.
#
# Оригинальный хелпер Stereolabs искал только media-ctl «zedx» (стерео ZED X)
# и молчал на ZED X One 4K (zedxone_uhd / IMX678). Здесь:
#   1) SN из kernel probe (zedx и zedxone_uhd)
#   2) если есть контрол — V4L2 zed_eeprom_data
#
# На плате:  ./bind_dev_to_sn.sh
# Если dmesg закрыт:  sudo ./bind_dev_to_sn.sh

set -u

kernel_log() {
    local out
    out="$(dmesg 2>/dev/null)" && [[ -n "${out}" ]] && {
        printf '%s\n' "${out}"
        return 0
    }
    if command -v journalctl >/dev/null 2>&1; then
        out="$(journalctl -k -b --no-pager 2>/dev/null)" && [[ -n "${out}" ]] && {
            printf '%s\n' "${out}"
            return 0
        }
    fi
    return 1
}

sn_from_eeprom_hex() {
    local hex="$1"
    hex="${hex// /}"
    hex="${hex//:/}"
    [[ "${#hex}" -ge 8 ]] || return 1
    local first_4="${hex:0:8}"
    local le="${first_4:6:2}${first_4:4:2}${first_4:2:2}${first_4:0:2}"
    local sn
    sn=$((16#${le})) || return 1
    [[ "${sn}" -gt 0 ]] || return 1
    echo "${sn}"
}

print_kernel_serials() {
    local log
    log="$(kernel_log)" || {
        echo "WARN: cannot read kernel log (try: sudo $0)" >&2
        return 1
    }

    # zedx 9-0020: zedx_probe: Serial Number : 47753729
    # zedxone_uhd 30-001a: imx678_probe: Serial Number 316861961
    declare -A drv_of bus_of addr_of sn_of
    local keys=()
    local line k drv bus addr sn

    while IFS= read -r line; do
        if [[ "${line}" =~ (zedxone_uhd|zedxone|zedone4k|zedx)[[:space:]]+([0-9]+)-([0-9a-fA-F]+):.*Serial\ Number[:\ ]+([0-9]+) ]]; then
            drv="${BASH_REMATCH[1]}"
            bus="${BASH_REMATCH[2]}"
            addr="${BASH_REMATCH[3]}"
            sn="${BASH_REMATCH[4]}"
            k="${bus}-${addr}"
            if [[ -z "${sn_of[${k}]+x}" ]]; then
                keys+=("${k}")
            fi
            drv_of["${k}"]="${drv}"
            bus_of["${k}"]="${bus}"
            addr_of["${k}"]="${addr}"
            sn_of["${k}"]="${sn}"
        fi
    done <<<"${log}"

    [[ "${#keys[@]}" -gt 0 ]] || return 1

    local sorted=()
    mapfile -t sorted < <(printf '%s\n' "${keys[@]}" | sort -t- -k1,1n -k2,2)

    local i=0
    for k in "${sorted[@]}"; do
        echo "ID:${i} BUS:${bus_of[${k}]} ADDR:${addr_of[${k}]} DRIVER:${drv_of[${k}]} SN:${sn_of[${k}]}"
        i=$((i + 1))
    done
}

print_v4l2_eeprom() {
    local v4l2_util
    v4l2_util="$(command -v v4l2-ctl || true)"
    [[ -n "${v4l2_util}" ]] || return 1

    local any=0
    local dev raw sn
    for dev in /dev/v4l-subdev* /dev/video*; do
        [[ -e "${dev}" ]] || continue
        raw="$("${v4l2_util}" -d "${dev}" -C zed_eeprom_data 2>/dev/null | cut -d "'" -f 2 || true)"
        [[ -n "${raw}" ]] || continue
        sn="$(sn_from_eeprom_hex "${raw}" || true)"
        [[ -n "${sn}" ]] || continue
        echo "DEV:${dev} SN:${sn}"
        any=1
    done
    [[ "${any}" -eq 1 ]]
}

echo "=== Kernel probe (ZED X / ZED X One 4K) ==="
if print_kernel_serials; then
    :
else
    echo "(no Serial Number lines for zedx / zedxone_uhd in this boot log)"
fi

echo
echo "=== V4L2 EEPROM (zed_eeprom_data), if the control exists ==="
if print_v4l2_eeprom; then
    :
else
    echo "(no zed_eeprom_data on /dev/video* or /dev/v4l-subdev*)"
fi

if ! command -v v4l2-ctl >/dev/null 2>&1; then
    echo
    echo "HINT: v4l2-ctl not found. Optional: sudo apt install v4l-utils" >&2
fi

if [[ ! -d /sys/firmware/devicetree/base/tegra-camera-platform ]]; then
    echo
    echo "HINT: this does not look like a Jetson camera DT. Run the script on the board." >&2
fi
