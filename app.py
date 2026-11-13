import copy
import codecs
import http.client
import json
import math
import os
import re
import socket
import ssl
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import manifold3d
from PySide6 import QtCore, QtGui, QtWidgets
import vtkmodules.qt

vtkmodules.qt.PyQtImpl = "PySide6"
import vtk
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy


class PrinterDiscoveryWorker(QtCore.QThread):
    devices_found = QtCore.Signal(list, list)

    def __init__(self, parent=None, include_network=True, timeout_ms=12000):
        super().__init__(parent)
        import queue

        self.include_network = bool(include_network)
        self.timeout_ms = max(1500, min(15000, int(timeout_ms)))
        self._service_events = queue.Queue(maxsize=256)
        self._service_overflow = False

    def _service_event(self, zeroconf, service_type, name, state_change):
        import queue

        if self.isInterruptionRequested():
            return
        try:
            self._service_events.put_nowait((service_type, name, state_change.name))
        except queue.Full:
            self._service_overflow = True

    def run(self):
        import hashlib
        import ipaddress
        import json
        import queue
        import re
        import subprocess
        import time
        from urllib.parse import quote
        from urllib.request import ProxyHandler, build_opener

        devices = {}
        errors = []
        started = time.monotonic()
        deadline = started+self.timeout_ms/1000
        browser = None
        zeroconf = None
        process = None
        service_names = {}
        brand_pattern = re.compile(
            r"3d[\s_-]*(?:print|프린터)|formlabs|form[\s_-]*[234](?:b|l|bl)?\b|"
            r"prusa|bambu|anycubic|elegoo|creality|raise[\s_-]*3d|flashforge|"
            r"zortrax|ultimaker|snapmaker|phrozen|asiga|sprintray|ackuretta|"
            r"shining[\s_-]*3d|uniz|nextdent|carima|microlay|nexa[\s_-]*3d|"
            r"rayshape|shera[\s_-]*print|halot|ender[\s_-]*[23567]|kobra|mars[\s_-]*[2345]|saturn",
            re.IGNORECASE,
        )
        try:
            if self.include_network and not self.isInterruptionRequested():
                try:
                    from zeroconf import IPVersion, ServiceBrowser, Zeroconf

                    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
                    browser = ServiceBrowser(
                        zeroconf,
                        ["_octoprint._tcp.local.", "_moonraker._tcp.local."],
                        handlers=[self._service_event],
                    )
                except ImportError:
                    errors.append("네트워크 프린터 검색 모듈이 없습니다. USB·Windows 장치만 확인했습니다.")
                except Exception as error:
                    errors.append("로컬 네트워크 서비스 검색을 시작하지 못했습니다: "+str(error)[:180])

            if not self.isInterruptionRequested():
                script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$queryErrors = @()
$queues = @()
$devices = @()
try {
    $queues = @(Get-CimInstance -ClassName Win32_Printer -Property Name,DriverName,PortName,WorkOffline,PrinterStatus,Local,Network -OperationTimeoutSec 3 -ErrorAction Stop |
        Select-Object Name, DriverName, PortName, WorkOffline, PrinterStatus, Local, Network)
} catch { $queryErrors += 'Windows 인쇄 대기열을 읽지 못했습니다.' }
$queueJson = [pscustomobject]@{ queues = $queues; devices = @(); errors = $queryErrors } |
    ConvertTo-Json -Depth 4 -Compress
[Console]::WriteLine($queueJson)
[Console]::Out.Flush()
$queryErrors = @()
try {
    $filter = "PNPClass='Printer' OR PNPClass='Ports' OR PNPClass='USB' OR PNPClass='3DPrinter'"
    $devices = @(Get-CimInstance -ClassName Win32_PnPEntity -Filter $filter -Property Name,Manufacturer,DeviceID,PNPClass,Status,ConfigManagerErrorCode,Present -OperationTimeoutSec 8 -ErrorAction Stop |
        Where-Object { $_.Present -ne $false } |
        Select-Object Name, Manufacturer, DeviceID, PNPClass, Status, ConfigManagerErrorCode)
} catch { $queryErrors += 'USB·직렬 포트 장치를 읽지 못했습니다.' }
[pscustomobject]@{ queues = @(); devices = $devices; errors = $queryErrors } |
    ConvertTo-Json -Depth 4 -Compress
'''
                powershell_budget = min(10.0, max(.1, deadline-time.monotonic()-.35))
                process = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                powershell_deadline = time.monotonic()+powershell_budget
                raw = b""
                while not self.isInterruptionRequested() and time.monotonic() < powershell_deadline:
                    try:
                        raw, unused_stderr = process.communicate(timeout=min(.10, max(.01, powershell_deadline-time.monotonic())))
                        break
                    except subprocess.TimeoutExpired as error:
                        if error.output:
                            raw = error.output
                        continue
                else:
                    process.kill()
                    try:
                        raw, unused_stderr = process.communicate(timeout=.5)
                    except subprocess.TimeoutExpired as error:
                        if error.output:
                            raw = error.output
                    if not self.isInterruptionRequested():
                        errors.append("Windows 장치 조회 시간이 초과되었습니다. 다시 검색할 수 있습니다.")
                if raw and not self.isInterruptionRequested():
                    payload = {"queues": [], "devices": [], "errors": []}
                    for line in raw.splitlines():
                        if not line.strip():
                            continue
                        try:
                            stage = json.loads(line.decode("utf-8-sig"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            errors.append("Windows 장치 일부 결과를 해석하지 못했습니다. 확인된 목록만 표시합니다.")
                            continue
                        if not isinstance(stage, dict):
                            continue
                        for key in payload:
                            values = stage.get(key) or []
                            payload[key].extend([values] if isinstance(values, dict) else values if isinstance(values, list) else [])
                    errors.extend(str(value)[:180] for value in payload.get("errors", []) if value)
                    queues = payload.get("queues") or []
                    if isinstance(queues, dict):
                        queues = [queues]
                    for item in queues:
                        name = str(item.get("Name") or "Windows 프린터")[:180]
                        driver = str(item.get("DriverName") or "")[:180]
                        port = str(item.get("PortName") or "")[:240]
                        candidate = bool(brand_pattern.search(name+" "+driver))
                        identity = "queue:"+hashlib.sha256((name+"\n"+port).encode("utf-8")).hexdigest()[:24]
                        devices[identity] = {
                            "id": identity,
                            "name": name,
                            "model": driver or name,
                            "source": "Windows 인쇄 대기열",
                            "connection": "LAN / Windows 대기열" if item.get("Network") else "Windows 대기열",
                            "address": port,
                            "backend": "windows_queue",
                            "status": "오프라인으로 등록됨" if item.get("WorkOffline") else "대기열 등록 · 실제 연결 미확인",
                            "capabilities": ["discover"],
                            "supported": False,
                            "is_3d_candidate": candidate,
                            "reason": "Windows 대기열 등록만 확인했습니다. 3D 출력 형식과 장치 통신은 아직 확인하지 않았습니다."
                            if candidate else "일반 Windows 프린터 대기열입니다. 3D 프린터로 확인되지 않았습니다.",
                        }
                    entities = payload.get("devices") or []
                    if isinstance(entities, dict):
                        entities = [entities]
                    for item in entities:
                        name = str(item.get("Name") or "USB 장치")[:180]
                        maker = str(item.get("Manufacturer") or "")[:180]
                        kind = str(item.get("PNPClass") or "")
                        device_id = str(item.get("DeviceID") or "")
                        candidate = bool(brand_pattern.search(name+" "+maker)) or kind.lower() == "3dprinter"
                        serial = re.search(r"\bCOM\d+\b", name, re.IGNORECASE)
                        if not candidate and not (kind.lower() == "ports" and serial):
                            continue
                        identity = "pnp:"+hashlib.sha256((device_id or name).encode("utf-8")).hexdigest()[:24]
                        error_code = item.get("ConfigManagerErrorCode")
                        if error_code == 45:
                            status = "현재 연결되지 않음"
                        elif error_code == 22:
                            status = "Windows에서 비활성화됨"
                        elif error_code not in [None, 0]:
                            status = f"Windows 장치 오류 {error_code}"
                        else:
                            status = "USB 장치 인식 · 호환 확인 필요" if candidate else "직렬 포트 인식 · 기종 미확인"
                        address = serial.group(0).upper() if serial else "\\".join(device_id.split("\\")[:2])
                        devices[identity] = {
                            "id": identity,
                            "name": name,
                            "model": name,
                            "source": "Windows USB / PnP",
                            "connection": "USB / 직렬 포트" if serial else "USB / PnP",
                            "address": address,
                            "backend": "serial_unverified" if serial else "usb_unverified",
                            "status": status,
                            "capabilities": ["discover"],
                            "supported": False,
                            "is_3d_candidate": candidate,
                            "reason": "장치 이름만 확인했습니다. 제조사 통신 연결이 필요합니다."
                            if candidate else "COM 포트는 프린터 외 장치일 수 있습니다. 3D 프린터인지 확인해야 합니다.",
                        }
                elif process.returncode not in [None, 0] and not self.isInterruptionRequested():
                    errors.append("Windows 장치 목록을 읽지 못했습니다.")

            if not self.isInterruptionRequested() and deadline-time.monotonic() > .25:
                try:
                    opener = build_opener(ProxyHandler({}))
                    with opener.open("http://127.0.0.1:44388/v1/device/list/", timeout=min(.7, deadline-time.monotonic()-.15)) as response:
                        raw_preform = response.read(2*1024*1024+1)
                    if len(raw_preform) <= 2*1024*1024:
                        preform = json.loads(raw_preform.decode("utf-8-sig"))
                        for item in preform.get("devices", [])[:128]:
                            if self.isInterruptionRequested():
                                break
                            if not isinstance(item, dict):
                                continue
                            device_id = str(item.get("id") or "")[:240]
                            if not device_id:
                                continue
                            connected = item.get("is_connected") is True
                            ready = item.get("ready_to_print_now") is True
                            name = str(item.get("product_name") or "Formlabs 프린터")[:180]
                            reported_status = str(item.get("status") or "")[:160]
                            status = "연결됨 · PreForm에서 출력 준비됨" if connected and ready else "연결됨 · PreForm 설정 확인 필요" if connected else "PreForm 등록 · 현재 연결되지 않음"
                            if reported_status:
                                status += " ("+reported_status+")"
                            identity = "formlabs:"+device_id
                            devices[identity] = {
                                "id": identity,
                                "name": name,
                                "model": name,
                                "source": "실행 중인 PreForm API",
                                "connection": str(item.get("connection_type") or "PreForm")[:80],
                                "address": "http://localhost:44388",
                                "backend": "formlabs",
                                "status": status,
                                "capabilities": ["discover", "status"],
                                "supported": False,
                                "is_3d_candidate": True,
                                "reason": "PreForm 출력 설정 연동이 필요합니다. 장치 준비 상태가 프로그램의 출력 전송 지원을 뜻하지는 않습니다.",
                                "device_id": device_id,
                                "is_connected": connected,
                                "ready_to_print_now": ready,
                            }
                except Exception:
                    pass

            if browser is not None:
                resolve_deadline = deadline-.25
                resolved = set()
                while not self.isInterruptionRequested() and time.monotonic() < resolve_deadline:
                    while True:
                        try:
                            service_type, name, state = self._service_events.get_nowait()
                        except queue.Empty:
                            break
                        identity = (service_type, name)
                        resolved.discard(identity)
                        if state == "Removed":
                            service_names.pop(identity, None)
                            devices.pop("mdns:"+hashlib.sha256((service_type+name).encode("utf-8")).hexdigest()[:24], None)
                        else:
                            service_names[identity] = True
                    pending = [identity for identity in service_names if identity not in resolved]
                    if not pending:
                        self.msleep(40)
                        continue
                    service_type, name = pending[0]
                    resolved.add((service_type, name))
                    if len(resolved) > 32:
                        errors.append("네트워크 서비스가 많아 일부 결과만 표시합니다.")
                        break
                    remaining_ms = int((resolve_deadline-time.monotonic())*1000)
                    if remaining_ms < 25:
                        break
                    info = zeroconf.get_service_info(service_type, name, timeout=min(350, remaining_ms))
                    if info is None:
                        continue
                    addresses = []
                    for value in info.parsed_addresses():
                        try:
                            address = ipaddress.ip_address(value)
                        except ValueError:
                            continue
                        if not address.is_unspecified and not address.is_multicast:
                            addresses.append(str(address))
                    if not addresses or not 0 < int(info.port) < 65536:
                        continue
                    properties = {}
                    for key, value in info.properties.items():
                        text_key = key.decode("utf-8", "replace") if isinstance(key, bytes) else str(key)
                        if text_key.lower() not in ["model", "vendor", "version", "path", "route_prefix", "usessl"]:
                            continue
                        text_value = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")
                        properties[text_key.lower()] = "".join(character for character in text_value if character.isprintable())[:240]
                    backend = "octoprint" if service_type == "_octoprint._tcp.local." else "moonraker"
                    label = "OctoPrint" if backend == "octoprint" else "Moonraker / Klipper"
                    prefix = properties.get("path", "") if backend == "octoprint" else properties.get("route_prefix", "")
                    if prefix and (not prefix.startswith("/") or "://" in prefix or prefix.startswith("//")):
                        prefix = ""
                    prefix = quote(prefix.rstrip("/"), safe="/-._~")
                    scheme = "https" if properties.get("usessl", "").lower() in ["1", "true", "yes"] else "http"
                    address = f"{scheme}://{addresses[0]}:{int(info.port)}{prefix}"
                    identity = "mdns:"+hashlib.sha256((service_type+name).encode("utf-8")).hexdigest()[:24]
                    display_name = name[:-len(service_type)].rstrip(".")[:180] or label
                    devices[identity] = {
                        "id": identity,
                        "name": display_name,
                        "model": properties.get("model") or label,
                        "source": "로컬 네트워크 mDNS",
                        "connection": "LAN / Wi-Fi",
                        "address": address,
                        "backend": backend,
                        "status": "제어 서비스 발견 · 프린터 연결 미확인",
                        "capabilities": ["discover"],
                        "supported": False,
                        "is_3d_candidate": True,
                        "reason": "프린터 제어 서비스만 발견했습니다. 장치 상태와 인증을 확인한 뒤 연결할 수 있습니다.",
                    }
            if self._service_overflow:
                errors.append("검색 응답이 많아 일부 서비스를 생략했습니다. 다시 검색할 수 있습니다.")
        except Exception as error:
            if not self.isInterruptionRequested():
                errors.append("프린터 검색 중 오류가 발생했습니다: "+str(error)[:240])
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                try:
                    process.communicate(timeout=.5)
                except subprocess.TimeoutExpired:
                    pass
            if browser is not None:
                try:
                    browser.cancel()
                except Exception:
                    pass
            if zeroconf is not None:
                try:
                    zeroconf.close()
                except Exception:
                    pass
            result = sorted(devices.values(), key=lambda item: (not item.get("is_3d_candidate", False), item["name"].casefold(), item["id"]))
            if self.isInterruptionRequested():
                errors.append("프린터 검색을 중단했습니다.")
            self.devices_found.emit(result, list(dict.fromkeys(errors)))


class PrinterConnectionWorker(QtCore.QThread):
    result = QtCore.Signal(dict)
    failed = QtCore.Signal(str)
    progress = QtCore.Signal(str)

    class _WorkerError(Exception):
        pass

    def __init__(self, profile, mode="check", file_path="", api_key="", parent=None):
        super().__init__(parent)
        self.profile = {
            field: profile.get(field, "")
            for field in ("id", "name", "backend", "address", "connection", "source", "device_id", "serial")
        }
        self.mode = mode
        self.file_path = str(file_path)
        self.api_key = str(api_key).strip()
        self.base_url = ""
        self.opener = None
        self.transmission_started = False

    def _check_cancelled(self):
        if self.isInterruptionRequested():
            raise InterruptedError()

    def _request_json(self, method, endpoint, data=None, extra_headers=None):
        self._check_cancelled()
        headers = {"Accept": "application/json", "User-Agent": "EXOCADE/1.0"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(self.base_url + endpoint, data=data, headers=headers, method=method)
        with self.opener.open(request, timeout=5) as response:
            self._check_cancelled()
            if response.status not in (200, 201):
                raise self._WorkerError("장비 응답 상태를 확인할 수 없습니다.")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) < 0 or int(content_length) > 1024 * 1024:
                        raise self._WorkerError("장비 응답이 허용 크기를 초과했습니다.")
                except (TypeError, ValueError, OverflowError):
                    raise self._WorkerError("장비 응답 형식이 올바르지 않습니다.") from None
            chunks = []
            length = 0
            read_chunk = getattr(response, "read1", response.read)
            while True:
                self._check_cancelled()
                chunk = read_chunk(min(65536, 1024 * 1024 + 1 - length))
                if not chunk:
                    break
                length += len(chunk)
                if length > 1024 * 1024:
                    raise self._WorkerError("장비 응답이 허용 크기를 초과했습니다.")
                chunks.append(chunk)
            self._check_cancelled()
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8-sig"))
        except (UnicodeError, ValueError, RecursionError):
            raise self._WorkerError("장비가 올바른 JSON 응답을 보내지 않았습니다.") from None
        if not isinstance(payload, dict) or payload.get("error"):
            raise self._WorkerError("장비가 요청을 처리하지 못했습니다.")
        return payload

    def _check_backend(self, backend):
        self._check_cancelled()
        checked = dict(self.profile)
        checked.update({
            "success": True, "backend": backend, "address": self.base_url,
            "connected": False, "ready": False, "upload_supported": False,
            "machine_state": "unknown", "remote_path": "", "mode": self.mode,
            "status_stale": False, "requires_recheck": False,
        })
        if backend == "octoprint":
            connection = self._request_json("GET", "/api/connection")
            current = connection.get("current")
            if not isinstance(current, dict) or not isinstance(current.get("state"), str):
                raise self._WorkerError("OctoPrint 연결 응답을 확인할 수 없습니다.")
            job = self._request_json("GET", "/api/job")
            if not isinstance(job.get("state"), str):
                raise self._WorkerError("OctoPrint 작업 상태를 확인할 수 없습니다.")
            connection_state = current["state"].strip().lower()
            job_state = job["state"].strip().lower()
            active_states = {"operational", "printing", "paused", "pausing", "starting", "cancelling", "finishing"}
            checked["connected"] = bool(current.get("port")) and connection_state in active_states
            checked["ready"] = checked["connected"] and connection_state == "operational" and job_state == "operational"
            if connection_state.startswith("error") or job_state.startswith("error"):
                checked["machine_state"] = "error"
            elif job_state in {"printing", "starting", "finishing", "cancelling"}:
                checked["machine_state"] = "printing"
            elif job_state in {"paused", "pausing"}:
                checked["machine_state"] = "paused"
            elif checked["ready"]:
                checked["machine_state"] = "ready"
            elif connection_state in {"offline", "closed", "closed with error"}:
                checked["machine_state"] = "offline"
            elif connection_state.startswith("connecting") or connection_state.startswith("detecting"):
                checked["machine_state"] = "connecting"
            checked["upload_supported"] = bool(self.api_key)
            checked["message"] = "OctoPrint 연결을 확인했습니다. 파일 전송 권한은 전송 시 확인됩니다."
            if not self.api_key:
                checked["message"] = "OctoPrint 상태를 확인했습니다. 파일 전송에는 API 키가 필요합니다."
        elif backend == "moonraker":
            info_response = self._request_json("GET", "/printer/info")
            info = info_response.get("result")
            if not isinstance(info, dict) or info.get("state") not in {"ready", "startup", "error", "shutdown"}:
                raise self._WorkerError("Moonraker 프린터 응답을 확인할 수 없습니다.")
            state_response = self._request_json("GET", "/printer/objects/query?webhooks&print_stats")
            state_result = state_response.get("result")
            status = state_result.get("status") if isinstance(state_result, dict) else None
            if not isinstance(status, dict) or not isinstance(status.get("webhooks"), dict) or not isinstance(status.get("print_stats"), dict):
                raise self._WorkerError("Moonraker 작업 상태를 확인할 수 없습니다.")
            webhooks_state = status["webhooks"].get("state")
            print_state = status["print_stats"].get("state")
            if webhooks_state not in {"ready", "startup", "error", "shutdown"} or print_state not in {"standby", "printing", "paused", "error", "complete", "cancelled"}:
                raise self._WorkerError("Moonraker 작업 상태를 확인할 수 없습니다.")
            checked["connected"] = info["state"] == "ready" and webhooks_state == "ready"
            checked["ready"] = checked["connected"] and print_state in {"standby", "complete", "cancelled"}
            checked["upload_supported"] = True
            if info["state"] in {"error", "shutdown"} or webhooks_state in {"error", "shutdown"} or print_state == "error":
                checked["machine_state"] = "error"
            elif not checked["connected"]:
                checked["machine_state"] = "connecting"
            elif print_state in {"printing", "paused"}:
                checked["machine_state"] = print_state
            elif checked["ready"]:
                checked["machine_state"] = "ready"
            checked["message"] = "Moonraker 연결을 확인했습니다. 파일 전송 권한은 전송 시 확인됩니다."
        elif backend == "formlabs":
            device_response = self._request_json("GET", "/v1/device/list/")
            devices = device_response.get("devices")
            if not isinstance(devices, list):
                raise self._WorkerError("PreForm 장치 목록을 확인할 수 없습니다.")
            device_id = str(self.profile.get("device_id") or self.profile.get("serial") or "").strip()
            if not device_id and str(self.profile.get("id", "")).startswith("formlabs:"):
                device_id = str(self.profile["id"]).split(":", 1)[1]
            selected = next((item for item in devices if isinstance(item, dict) and item.get("id") == device_id), None)
            if not device_id:
                checked["message"] = "PreForm API에 연결됐습니다. 장치 목록에서 프린터를 선택하십시오."
            elif selected is None:
                checked["machine_state"] = "offline"
                checked["message"] = "선택한 Formlabs 장치가 현재 연결 목록에 없습니다."
            else:
                checked["device_id"] = device_id
                checked["connected"] = selected.get("is_connected") is True and selected.get("connection_type") not in {"VIRTUAL", ""}
                checked["ready"] = checked["connected"] and selected.get("ready_to_print_now") is True
                checked["machine_state"] = "ready" if checked["ready"] else "connected" if checked["connected"] else "offline"
                checked["message"] = "Formlabs 장치를 확인했습니다. 출력 준비와 전송은 PreForm에서 진행하십시오."
        else:
            checked["success"] = False
            checked["message"] = "선택한 장치는 직접 파일 전송을 지원하지 않습니다."
        return checked

    def _multipart_chunks(self, file_handle, prefix, suffix, size):
        self._check_cancelled()
        self.transmission_started = True
        yield prefix
        remaining = size
        while remaining:
            self._check_cancelled()
            chunk = file_handle.read(min(65536, remaining))
            if not chunk:
                raise self._WorkerError("전송 중 파일 크기가 변경됐습니다.")
            remaining -= len(chunk)
            yield chunk
        self._check_cancelled()
        if file_handle.read(1):
            raise self._WorkerError("전송 중 파일 크기가 변경됐습니다.")
        yield suffix

    def run(self):
        failure_message = ""
        try:
            self._check_cancelled()
            if self.mode not in {"check", "upload"}:
                raise self._WorkerError("지원하지 않는 연결 작업입니다.")
            if len(self.api_key) > 4096 or any(ord(character) < 32 or ord(character) > 126 for character in self.api_key):
                raise self._WorkerError("API 키 형식이 올바르지 않습니다.")
            backend = str(self.profile.get("backend") or "auto").strip().lower()
            if backend in {"windows_queue", "serial_unverified", "usb_unverified"}:
                backend = "unverified"
            if backend not in {"auto", "octoprint", "moonraker", "formlabs", "unverified"}:
                raise self._WorkerError("지원하지 않는 프린터 연결 방식입니다.")
            if backend == "unverified":
                self.result.emit(self._check_backend(backend))
                return
            address = str(self.profile.get("address") or "").strip()
            if not address or any(character.isspace() or ord(character) < 32 for character in address) or any(character in address for character in ("\\", "?", "#")):
                raise self._WorkerError("프린터의 HTTP 또는 HTTPS 주소를 입력하십시오.")
            if "://" not in address:
                address = "http://" + address
            parsed = urllib.parse.urlsplit(address)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                raise self._WorkerError("인증정보와 쿼리가 없는 HTTP 또는 HTTPS 주소를 입력하십시오.")
            try:
                port = parsed.port
                host = parsed.hostname.encode("idna").decode("ascii")
            except (UnicodeError, ValueError):
                raise self._WorkerError("프린터 주소 또는 포트가 올바르지 않습니다.") from None
            if not host or any(character in host for character in ("/", "\\", "@", "%")) or (port is not None and not 1 <= port <= 65535):
                raise self._WorkerError("프린터 주소 또는 포트가 올바르지 않습니다.")
            decoded_path = urllib.parse.unquote(parsed.path)
            if any(part in {".", ".."} for part in decoded_path.split("/")) or any(ord(character) < 32 for character in decoded_path):
                raise self._WorkerError("프린터 주소 경로가 올바르지 않습니다.")
            authority = "[" + host + "]" if ":" in host else host
            if port is not None:
                authority += ":" + str(port)
            self.base_url = urllib.parse.urlunsplit((parsed.scheme.lower(), authority, urllib.parse.quote(decoded_path.rstrip("/"), safe="/-._~"), "", ""))

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, request, file_pointer, code, message, headers, new_url):
                    return None

            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirect(),
                urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            )
            self.progress.emit("선택한 프린터의 연결 상태를 확인하고 있습니다.")
            if backend == "auto":
                checked = None
                for candidate in ("octoprint", "moonraker"):
                    self._check_cancelled()
                    try:
                        checked = self._check_backend(candidate)
                        break
                    except urllib.error.HTTPError as error:
                        if error.code in {401, 403}:
                            raise
                        error.close()
                    except (self._WorkerError, ValueError, urllib.error.URLError, socket.timeout, TimeoutError, http.client.HTTPException):
                        continue
                if checked is None:
                    raise self._WorkerError("입력한 주소에서 지원하는 프린터 API를 확인하지 못했습니다.")
            else:
                checked = self._check_backend(backend)
            self._check_cancelled()
            if self.mode == "check":
                self.result.emit(checked)
                return
            if not checked.get("success") or checked.get("backend") not in {"octoprint", "moonraker"} or not checked.get("upload_supported"):
                raise self._WorkerError("연결이 확인된 지원 프린터만 파일을 전송할 수 있습니다.")
            if checked.get("connected") is not True:
                raise self._WorkerError("프린터 연결이 끊어졌습니다. 장비 연결을 다시 확인한 후 전송하십시오.")
            if checked.get("ready") is not True:
                raise self._WorkerError("프린터가 준비 상태가 아닙니다. 현재 작업과 장비 상태를 확인하십시오.")
            path = Path(self.file_path)
            if path.suffix.lower() != ".gcode":
                raise self._WorkerError("슬라이서에서 준비한 텍스트 G-code 파일을 선택하십시오.")
            maximum = 256 * 1024 * 1024
            with path.open("rb") as file_handle:
                initial_stat = os.fstat(file_handle.fileno())
                size = initial_stat.st_size
                if not 0 < size <= maximum:
                    raise self._WorkerError("G-code 파일은 0바이트보다 크고 256MB 이하여야 합니다.")
                self.progress.emit("전송할 G-code 파일을 확인하고 있습니다.")
                decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
                command_found = False
                tail = ""
                read_size = 0
                while True:
                    self._check_cancelled()
                    chunk = file_handle.read(65536)
                    if not chunk:
                        break
                    read_size += len(chunk)
                    if read_size > size or read_size > maximum:
                        raise self._WorkerError("확인 중 G-code 파일 크기가 변경됐습니다.")
                    try:
                        text = decoder.decode(chunk)
                    except UnicodeError:
                        raise self._WorkerError("UTF-8 텍스트 G-code 파일만 전송할 수 있습니다.") from None
                    if any(ord(character) < 32 and character not in "\r\n\t" for character in text):
                        raise self._WorkerError("바이너리 데이터가 포함된 파일은 전송할 수 없습니다.")
                    if not command_found and re.search(r"(?mi)^[ \t]*(?:N[0-9]+[ \t]*)?[GMT][0-9]+", tail + text):
                        command_found = True
                    tail = text[-128:]
                try:
                    decoder.decode(b"", final=True)
                except UnicodeError:
                    raise self._WorkerError("UTF-8 텍스트 G-code 파일만 전송할 수 있습니다.") from None
                latest_stat = os.fstat(file_handle.fileno())
                if read_size != size or latest_stat.st_size != size or latest_stat.st_mtime_ns != initial_stat.st_mtime_ns:
                    raise self._WorkerError("확인 중 G-code 파일이 변경됐습니다. 다시 선택하십시오.")
                if not command_found:
                    raise self._WorkerError("G-code 명령을 확인하지 못했습니다. 슬라이서 출력 파일을 선택하십시오.")
                checked = self._check_backend(checked["backend"])
                if checked.get("connected") is not True or checked.get("ready") is not True or not checked.get("upload_supported"):
                    raise self._WorkerError("프린터가 전송 준비 상태가 아닙니다. 장비 상태를 다시 확인하십시오.")
                file_handle.seek(0)
                self._check_cancelled()
                boundary = "EXOCADE" + uuid.uuid4().hex
                safe_stem = re.sub(r"[^A-Za-z0-9_-]", "_", path.stem).strip("_")[:60] or "print"
                remote_name = safe_stem + "_" + uuid.uuid4().hex[:12] + ".gcode"
                fields = [("select", "false"), ("print", "false")] if checked["backend"] == "octoprint" else [("root", "gcodes"), ("print", "false")]
                prefix_parts = []
                for name, value in fields:
                    prefix_parts.append("--" + boundary + '\r\nContent-Disposition: form-data; name="' + name + '"\r\n\r\n' + value + "\r\n")
                prefix_parts.append("--" + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="' + remote_name + '"\r\nContent-Type: application/octet-stream\r\n\r\n')
                prefix = "".join(prefix_parts).encode("ascii")
                suffix = ("\r\n--" + boundary + "--\r\n").encode("ascii")
                endpoint = "/api/files/local" if checked["backend"] == "octoprint" else "/server/files/upload"
                self.progress.emit("G-code 파일을 전송하고 있습니다. 출력 시작은 요청하지 않습니다.")
                response = self._request_json("POST", endpoint, self._multipart_chunks(file_handle, prefix, suffix, size), {
                    "Content-Type": "multipart/form-data; boundary=" + boundary,
                    "Content-Length": str(len(prefix) + size + len(suffix)),
                })
                after_stat = os.fstat(file_handle.fileno())
                if after_stat.st_size != size or after_stat.st_mtime_ns != initial_stat.st_mtime_ns:
                    raise self._WorkerError("전송 중 파일이 변경됐습니다. 장비 파일 목록을 확인하십시오.")
            if checked["backend"] == "octoprint":
                files = response.get("files")
                uploaded = files.get("local") if isinstance(files, dict) else None
                if not isinstance(uploaded, dict) or response.get("done") is not True or response.get("effectivePrint") is not False or response.get("effectiveSelect") is not False:
                    raise self._WorkerError("업로드 완료와 출력 미시작 상태를 확인하지 못했습니다. 장비 파일 목록을 확인하십시오.")
                remote_path = uploaded.get("path") or uploaded.get("name")
            else:
                uploaded_response = response.get("result", response)
                uploaded = uploaded_response.get("item") if isinstance(uploaded_response, dict) else None
                if not isinstance(uploaded, dict) or uploaded.get("root") != "gcodes" or uploaded_response.get("print_started") is not False or uploaded_response.get("print_queued") is not False or uploaded_response.get("action") != "create_file":
                    raise self._WorkerError("업로드 완료와 출력 미시작 상태를 확인하지 못했습니다. 장비 파일 목록을 확인하십시오.")
                remote_path = uploaded.get("path")
            if not isinstance(remote_path, str) or not remote_path or len(remote_path) > 1024 or any(ord(character) < 32 for character in remote_path):
                raise self._WorkerError("업로드 파일 경로를 확인하지 못했습니다. 장비 파일 목록을 확인하십시오.")
            if Path(remote_path.replace("\\", "/")).name != remote_name:
                raise self._WorkerError("업로드 파일명이 요청과 다릅니다. 장비 파일 목록을 확인하십시오.")
            self._check_cancelled()
            checked.update({
                "success": True, "remote_path": remote_path,
                "status_stale": True, "requires_recheck": True,
                "message": "파일 전송을 완료했습니다. 출력은 시작하지 않았습니다. 장비 상태를 다시 확인하십시오.",
            })
            self.result.emit(checked)
        except InterruptedError:
            failure_message = "작업을 취소했습니다. 전송을 시작했다면 장비 파일 목록을 확인하십시오." if self.transmission_started else "연결 작업을 취소했습니다."
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status in {401, 403}:
                failure_message = "인증 또는 API 권한이 필요합니다. HTTP " + str(status)
            elif 300 <= status < 400:
                failure_message = "주소 이동 요청을 차단했습니다. 프린터의 최종 API 주소를 입력하십시오. HTTP " + str(status)
            else:
                failure_message = "프린터가 요청을 처리하지 못했습니다. HTTP " + str(status)
        except (ssl.SSLError, ssl.CertificateError):
            failure_message = "프린터의 HTTPS 인증서를 확인하지 못했습니다."
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLError):
                failure_message = "프린터의 HTTPS 인증서를 확인하지 못했습니다."
            else:
                failure_message = "프린터에 연결할 수 없습니다. 주소와 네트워크를 확인하십시오."
        except (socket.timeout, TimeoutError):
            failure_message = "프린터 응답 시간이 초과됐습니다."
        except self._WorkerError as error:
            failure_message = str(error)
        except ValueError:
            failure_message = "입력 또는 장비 응답 형식이 올바르지 않습니다."
        except (OSError, http.client.HTTPException):
            failure_message = "파일 또는 프린터 연결을 처리하지 못했습니다."
        except Exception:
            failure_message = "프린터 작업을 완료하지 못했습니다."
        finally:
            self.api_key = ""
            if failure_message:
                if self.transmission_started and "장비 파일 목록" not in failure_message:
                    failure_message += " 전송 여부는 장비 파일 목록에서 확인하십시오."
                self.failed.emit(failure_message)


class DentalWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EXOCADE · Dental Design Studio")
        self.resize(1550, 960)
        self.setMinimumSize(1180, 760)
        self.asset_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "assets"
        self.setWindowIcon(QtGui.QIcon(str(self.asset_dir / "icon.svg")))
        self.objects = {}
        self.teeth = {}
        self.current_tooth = 16
        self.active_id = None
        self.mode = "orbit"
        self.dragging = False
        self.dirty = False
        self.project_path = None
        self.undo_stack = []
        self.redo_stack = []
        self.margin_actor = None
        self.measure_actors = []
        self.measure_points = []
        self.demo_margins = {}
        self.scalar_bar = None
        self.busy = False
        self.section_plane = vtk.vtkPlane()
        self.section_plane.SetNormal(1, 0, 0)
        self.section_enabled = False
        self.color_mode = None
        self.last_export_paths = []
        self.layout_points = []
        self.path_actor = None
        self.ct_volume = None
        self.ct_metadata = None
        self.bed_actor = None
        self.printer_dialog = None
        self.printers = {}
        self.printer_checks = {}
        self.printer_api_keys = {}
        self._printer_discovery = None
        self._printer_request = None
        self._printer_request_key = ""
        self._printer_job_path = ""
        self._printer_job_target_id = ""
        self._printer_closing = False
        settings = QtCore.QSettings("EXOCADE", "DentalDesign")
        self.selected_printer_id = str(settings.value("selected_printer_id", ""))
        try:
            saved_printers = json.loads(str(settings.value("printer_profiles", "[]")))
            if isinstance(saved_printers, list):
                for profile in saved_printers[:256]:
                    if isinstance(profile, dict) and isinstance(profile.get("id"), str):
                        restored = {field: profile[field] for field in ("id", "name", "model", "source", "connection", "address", "backend", "device_id", "serial", "manual", "is_3d_candidate", "reason") if field in profile}
                        restored["status"] = "저장된 장치 · 연결 확인 필요"
                        self.printers[restored["id"]] = restored
        except (ValueError, TypeError):
            pass
        self.tooth_dimensions = json.loads((self.asset_dir / "tooth_dimensions.json").read_text(encoding="utf-8"))["teeth"]

        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QtWidgets.QFrame()
        header.setObjectName("header")
        header.setFixedHeight(70)
        h = QtWidgets.QHBoxLayout(header)
        h.setContentsMargins(20, 10, 20, 10)
        logo = QtWidgets.QLabel()
        logo.setPixmap(QtGui.QIcon(str(self.asset_dir / "icon.svg")).pixmap(38, 38))
        h.addWidget(logo)
        brand_box = QtWidgets.QVBoxLayout()
        brand_box.setSpacing(0)
        brand = QtWidgets.QLabel("EXOCADE")
        brand.setObjectName("brand")
        brand_box.addWidget(brand)
        subtitle = QtWidgets.QLabel("DENTAL DESIGN STUDIO")
        subtitle.setObjectName("subtitle")
        brand_box.addWidget(subtitle)
        h.addLayout(brand_box)
        h.addSpacing(35)
        self.case_name = QtWidgets.QLineEdit("새 케이스")
        self.case_name.setPlaceholderText("케이스 이름")
        self.case_name.setMaximumWidth(240)
        self.case_name.textEdited.connect(lambda: setattr(self, "dirty", True))
        h.addWidget(self.case_name)
        h.addStretch()
        for text, callback, shortcut in [
            ("새 케이스", self.new_case, "Ctrl+N"),
            ("프로젝트 열기", self.open_project, "Ctrl+O"),
            ("저장", self.save_project, "Ctrl+S"),
            ("내보내기", self.export_mesh, "Ctrl+E"),
            ("프린터 선택", self.show_printer_dialog, "Ctrl+P"),
        ]:
            button = QtWidgets.QPushButton(text)
            button.setShortcut(shortcut)
            button.setToolTip(shortcut)
            if "내보내기" in text:
                button.setObjectName("primary")
            button.clicked.connect(callback)
            h.addWidget(button)
        layout.addWidget(header)

        steps = QtWidgets.QFrame()
        steps.setObjectName("toolbar")
        steps_layout = QtWidgets.QHBoxLayout(steps)
        steps_layout.setContentsMargins(16, 8, 16, 8)
        self.step_group = QtWidgets.QButtonGroup(self)
        self.step_buttons = []
        for index, title in enumerate(["스캔", "마진선", "삽입축", "보철 설계", "형태 편집", "검사", "모델·장치", "출력 준비", "DICOM"]):
            button = QtWidgets.QPushButton(f"{index + 1:02d}  {title}")
            button.setObjectName("stepButton")
            button.setCheckable(True)
            self.step_group.addButton(button, index)
            steps_layout.addWidget(button)
            self.step_buttons.append(button)
        self.step_group.idClicked.connect(self.set_step)
        layout.addWidget(steps)

        splitter = QtWidgets.QSplitter()
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)
        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(205)
        sidebar.setMaximumWidth(285)
        left = QtWidgets.QVBoxLayout(sidebar)
        left.setContentsMargins(14, 18, 14, 14)
        label = QtWidgets.QLabel("케이스 구성")
        label.setObjectName("panelTitle")
        left.addWidget(label)
        self.tooth_label = QtWidgets.QLabel("선택 치아  ·  16")
        self.tooth_label.setObjectName("sectionTitle")
        left.addWidget(self.tooth_label)
        self.tooth_buttons = {}
        tooth_grid = QtWidgets.QGridLayout()
        tooth_grid.setSpacing(3)
        quadrants = [[18, 17, 16, 15, 14, 13, 12, 11], [21, 22, 23, 24, 25, 26, 27, 28],
                     [48, 47, 46, 45, 44, 43, 42, 41], [31, 32, 33, 34, 35, 36, 37, 38]]
        for row, values in enumerate(quadrants):
            for column, tooth in enumerate(values):
                button = QtWidgets.QPushButton(str(tooth))
                button.setCheckable(True)
                button.setMinimumSize(20, 28)
                button.setMaximumHeight(30)
                button.setStyleSheet("padding: 1px; font-size: 10px;")
                button.setToolTip(f"FDI {tooth}번 치아")
                button.clicked.connect(lambda checked=False, t=tooth: self.select_tooth(t))
                tooth_grid.addWidget(button, row + (1 if row > 1 else 0), column)
                self.tooth_buttons[tooth] = button
        left.addLayout(tooth_grid)
        info = QtWidgets.QLabel("FDI 치아 번호 · 치아별 마진/보철 관리")
        info.setObjectName("hint")
        info.setWordWrap(True)
        left.addWidget(info)
        self.object_list = QtWidgets.QTreeWidget()
        self.object_list.setHeaderLabels(["오브젝트"])
        self.object_list.setRootIsDecorated(False)
        self.object_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.object_list.currentItemChanged.connect(self.selection_changed)
        self.object_list.itemChanged.connect(self.visibility_changed)
        left.addWidget(self.object_list, 1)
        label = QtWidgets.QLabel("선택 오브젝트 투명도")
        label.setObjectName("sectionTitle")
        left.addWidget(label)
        self.opacity = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.opacity.setRange(5, 100)
        self.opacity.setValue(100)
        self.opacity.valueChanged.connect(self.opacity_changed)
        left.addWidget(self.opacity)
        self.object_info = QtWidgets.QLabel("오브젝트를 선택하세요.")
        self.object_info.setObjectName("hint")
        self.object_info.setWordWrap(True)
        left.addWidget(self.object_info)
        button = QtWidgets.QPushButton("선택 오브젝트 삭제")
        button.clicked.connect(self.delete_selected)
        left.addWidget(button)
        splitter.addWidget(sidebar)

        center = QtWidgets.QWidget()
        center_layout = QtWidgets.QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        view_tools = QtWidgets.QFrame()
        view_tools.setObjectName("toolbar")
        view_layout = QtWidgets.QHBoxLayout(view_tools)
        view_layout.setContentsMargins(10, 7, 10, 7)
        for title, key in [("교합면", "top"), ("정면", "front"), ("좌", "left"), ("우", "right"), ("입체", "iso")]:
            button = QtWidgets.QPushButton(title)
            button.setMaximumWidth(62)
            button.clicked.connect(lambda checked=False, k=key: self.camera_view(k))
            view_layout.addWidget(button)
        view_layout.addStretch()
        for title, callback in [("전체 보기", lambda: self.camera_view("fit")), ("선택 확대", self.focus_selection),
                                ("실행 취소", self.undo), ("다시 실행", self.redo)]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            view_layout.addWidget(button)
        center_layout.addWidget(view_tools)
        self.vtk_widget = QVTKRenderWindowInteractor(center)
        center_layout.addWidget(self.vtk_widget, 1)
        self.renderer = vtk.vtkRenderer()
        self.renderer.SetBackground(0.055, 0.075, 0.11)
        self.renderer.SetBackground2(0.16, 0.21, 0.28)
        self.renderer.GradientBackgroundOn()
        self.renderer.UseFXAAOn()
        self.vtk_widget.GetRenderWindow().AddRenderer(self.renderer)
        self.vtk_widget.GetRenderWindow().SetMultiSamples(4)
        self.interactor = self.vtk_widget.GetRenderWindow().GetInteractor()
        self.style = vtk.vtkInteractorStyleTrackballCamera()
        self.interactor.SetInteractorStyle(self.style)
        self.style.AddObserver("LeftButtonPressEvent", self.mouse_press)
        self.style.AddObserver("MouseMoveEvent", self.mouse_move)
        self.style.AddObserver("LeftButtonReleaseEvent", self.mouse_release)
        self.picker = vtk.vtkCellPicker()
        self.picker.SetTolerance(0.002)
        self.renderer.GetActiveCamera().ParallelProjectionOn()
        axes = vtk.vtkAxesActor()
        axes.SetTotalLength(1.5, 1.5, 1.5)
        self.orientation = vtk.vtkOrientationMarkerWidget()
        self.orientation.SetOrientationMarker(axes)
        self.orientation.SetInteractor(self.interactor)
        self.orientation.SetViewport(0.0, 0.0, 0.15, 0.19)
        bottom = QtWidgets.QFrame()
        bottom.setObjectName("footer")
        bottom_layout = QtWidgets.QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(14, 8, 14, 8)
        self.mode_label = QtWidgets.QLabel("보기 모드")
        self.mode_label.setStyleSheet("color: #f3b459; font-weight: 600;")
        bottom_layout.addWidget(self.mode_label)
        bottom_layout.addStretch()
        hint = QtWidgets.QLabel("좌클릭 회전  ·  휠 확대  ·  가운데 버튼 이동  ·  F 전체 보기")
        hint.setObjectName("hint")
        bottom_layout.addWidget(hint)
        center_layout.addWidget(bottom)
        splitter.addWidget(center)

        right_frame = QtWidgets.QFrame()
        right_frame.setObjectName("inspector")
        right_frame.setMinimumWidth(280)
        right_frame.setMaximumWidth(350)
        right_layout = QtWidgets.QVBoxLayout(right_frame)
        right_layout.setContentsMargins(16, 18, 16, 12)
        self.panel_title = QtWidgets.QLabel("스캔 데이터")
        self.panel_title.setObjectName("panelTitle")
        right_layout.addWidget(self.panel_title)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.pages = QtWidgets.QStackedWidget()
        scroll.setWidget(self.pages)
        right_layout.addWidget(scroll, 1)
        self.page_layouts = []
        for i in range(9):
            page = QtWidgets.QWidget()
            box = QtWidgets.QVBoxLayout(page)
            box.setContentsMargins(0, 8, 0, 8)
            box.setSpacing(12)
            self.pages.addWidget(page)
            self.page_layouts.append(box)

        box = self.page_layouts[0]
        note = QtWidgets.QLabel("STL · PLY · OBJ · VTP\n모든 길이의 기준 단위는 mm입니다.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        box.addWidget(note)
        self.import_role = QtWidgets.QComboBox()
        for title, role in [("작업악 스캔", "scan"), ("대합치 스캔", "antagonist"), ("참고 모델", "reference"),
                            ("치아 STL 라이브러리", "library"), ("기성 임플란트·부품", "part")]:
            self.import_role.addItem(title, role)
        box.addWidget(self.import_role)
        button = QtWidgets.QPushButton("+ 스캔 파일 불러오기")
        button.setObjectName("primary")
        button.clicked.connect(self.import_mesh)
        box.addWidget(button)
        button = QtWidgets.QPushButton("예제 케이스 불러오기")
        button.clicked.connect(self.load_demo)
        box.addWidget(button)
        label = QtWidgets.QLabel("스캔 정리")
        label.setObjectName("sectionTitle")
        box.addWidget(label)
        for title, operation in [("중복 정점 정리 / 법선 정렬", "clean"), ("면 방향 전체 반전", "reverse"),
                                 ("작은 구멍 메우기", "holes"), ("메시 50% 경량화", "decimate")]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda checked=False, op=operation: self.repair_mesh(op))
            box.addWidget(button)
        self.hole_size = QtWidgets.QDoubleSpinBox()
        self.hole_size.setRange(0.1, 20)
        self.hole_size.setValue(1)
        self.hole_size.setSuffix(" mm · 구멍 반경 한도")
        box.addWidget(self.hole_size)
        label = QtWidgets.QLabel("선택 모델 좌표 변환")
        label.setObjectName("sectionTitle")
        box.addWidget(label)
        self.transform_controls = {}
        form = QtWidgets.QFormLayout()
        for key, title, lo, hi, initial, suffix in [
            ("tx", "이동 X", -1000, 1000, 0, " mm"), ("ty", "이동 Y", -1000, 1000, 0, " mm"),
            ("tz", "이동 Z", -1000, 1000, 0, " mm"), ("rx", "회전 X", -180, 180, 0, "°"),
            ("ry", "회전 Y", -180, 180, 0, "°"), ("rz", "회전 Z", -180, 180, 0, "°"),
            ("scale", "단위 배율", 0.001, 1000, 1, " ×"),
        ]:
            control = QtWidgets.QDoubleSpinBox()
            control.setRange(lo, hi)
            control.setDecimals(3 if key == "scale" else 2)
            control.setValue(initial)
            control.setSuffix(suffix)
            self.transform_controls[key] = control
            form.addRow(title, control)
        box.addLayout(form)
        button = QtWidgets.QPushButton("변환 적용")
        button.clicked.connect(self.transform_mesh)
        box.addWidget(button)

        box = self.page_layouts[1]
        self.margin_info = QtWidgets.QLabel("선택한 치아의 마진을 순서대로 클릭하세요.")
        self.margin_info.setWordWrap(True)
        self.margin_info.setObjectName("hint")
        box.addWidget(self.margin_info)
        for title, callback in [("마진 점 찍기", lambda: self.set_mode("margin")), ("마지막 점 취소", self.margin_undo),
                                ("마진 닫기", self.margin_close), ("마진 초기화", self.margin_clear)]:
            button = QtWidgets.QPushButton(title)
            if title == "마진 닫기":
                button.setObjectName("primary")
            button.clicked.connect(callback)
            box.addWidget(button)
        label = QtWidgets.QLabel("단면 기반 초기 제안")
        label.setObjectName("sectionTitle")
        box.addWidget(label)
        note = QtWidgets.QLabel("마진 높이의 스캔을 한 번 클릭한 뒤 단면 윤곽을 추출합니다. 결과를 확인하고 닫아주세요.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        box.addWidget(note)
        button = QtWidgets.QPushButton("단면 기준점 선택")
        button.clicked.connect(lambda: self.set_mode("seed"))
        box.addWidget(button)
        button = QtWidgets.QPushButton("선택 높이에서 윤곽 추출")
        button.clicked.connect(self.margin_section)
        box.addWidget(button)
        button = QtWidgets.QPushButton("예제 16번 마진 사용")
        button.clicked.connect(self.demo_margin)
        box.addWidget(button)
        button = QtWidgets.QPushButton("단면 윤곽의 곡률 보정 제안")
        button.clicked.connect(self.refine_margin_curvature)
        box.addWidget(button)

        box = self.page_layouts[2]
        note = QtWidgets.QLabel("교합면에서 지대치를 바라본 후 현재 시선으로 삽입축을 지정하세요. 스캔 좌표 Z축도 사용할 수 있습니다.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        box.addWidget(note)
        self.axis_info = QtWidgets.QLabel("X 0.000   Y 0.000   Z 1.000")
        self.axis_info.setWordWrap(True)
        self.axis_info.setObjectName("metric")
        self.axis_info.setStyleSheet("font-size: 13px; padding: 10px;")
        self.axis_info.setMinimumHeight(48)
        box.addWidget(self.axis_info)
        button = QtWidgets.QPushButton("역경사를 줄이는 축 제안")
        button.setObjectName("primary")
        button.clicked.connect(self.suggest_axis)
        box.addWidget(button)
        self.undercut_angle = QtWidgets.QDoubleSpinBox()
        self.undercut_angle.setRange(0, 30)
        self.undercut_angle.setValue(2)
        self.undercut_angle.setSuffix("° · 역경사 허용각")
        box.addWidget(self.undercut_angle)
        button = QtWidgets.QPushButton("표면 법선 역경사 맵")
        button.clicked.connect(self.show_undercuts)
        box.addWidget(button)
        for title, source in [("현재 시선을 삽입축으로", "camera"), ("스캔 +Z축 사용", "z"), ("스캔 −Z축 사용", "minus_z")]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda checked=False, s=source: self.set_axis(s))
            box.addWidget(button)
        note = QtWidgets.QLabel("삽입축 방향의 첫 표면을 내면 기준으로 사용합니다. 언더컷은 이 방향을 기준으로 차단됩니다.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        box.addWidget(note)

        box = self.page_layouts[3]
        self.restoration_type = QtWidgets.QComboBox()
        self.restoration_type.addItems(["해부학 크라운", "균일 두께 코핑", "인레이", "온레이", "베니어"])
        self.restoration_type.currentIndexChanged.connect(self.restoration_changed)
        box.addWidget(self.restoration_type)
        self.shape_type = QtWidgets.QComboBox()
        self.shape_type.addItems(["대구치 · 4교두", "소구치 · 2교두", "전치 · 절단연"])
        self.shape_type.setToolTip("절차적으로 생성하는 기본 형태입니다. 임상 치아 라이브러리가 아닙니다.")
        box.addWidget(self.shape_type)
        self.material = QtWidgets.QComboBox()
        self.material.addItems(["지르코니아 · 기본값", "리튬디실리케이트 · 기본값", "레진 · 기본값", "금속 · 기본값"])
        self.material.currentIndexChanged.connect(self.material_changed)
        box.addWidget(self.material)
        self.design_controls = {}
        form = QtWidgets.QFormLayout()
        for key, title, lo, hi, initial, step, suffix in [
            ("gap", "시멘트 공간", 0, 0.3, 0.06, 0.01, " mm"),
            ("wall", "설계 벽두께", 0.3, 3, 0.8, 0.1, " mm"),
            ("height", "교합면 추가 높이", 0, 5, 1.4, 0.1, " mm"),
            ("cusp", "교두 높이", 0, 2, 0.75, 0.05, " mm"),
            ("width", "외형 폭 배율", 1.0, 1.5, 1.08, 0.02, " ×"),
            ("edge", "마진 가장자리 폭", 0.05, 0.8, 0.15, 0.05, " mm"),
        ]:
            control = QtWidgets.QDoubleSpinBox()
            control.setRange(lo, hi)
            control.setDecimals(2)
            control.setSingleStep(step)
            control.setValue(initial)
            control.setSuffix(suffix)
            self.design_controls[key] = control
            form.addRow(title, control)
        box.addLayout(form)
        button = QtWidgets.QPushButton("크라운 생성 / 다시 생성")
        button.setObjectName("primary")
        button.clicked.connect(self.generate_crown)
        box.addWidget(button)
        label = QtWidgets.QLabel("치아 형태 라이브러리")
        label.setObjectName("sectionTitle")
        box.addWidget(label)
        self.library_combo = QtWidgets.QComboBox()
        self.library_combo.addItem("매개변수 기본 형태", None)
        box.addWidget(self.library_combo)
        form = QtWidgets.QFormLayout()
        self.library_scale = QtWidgets.QDoubleSpinBox()
        self.library_scale.setRange(.3, 3)
        self.library_scale.setValue(1)
        self.library_scale.setSingleStep(.05)
        self.library_rotation = QtWidgets.QDoubleSpinBox()
        self.library_rotation.setRange(-180, 180)
        self.library_rotation.setSuffix("°")
        form.addRow("라이브러리 배율", self.library_scale)
        form.addRow("라이브러리 회전", self.library_rotation)
        box.addLayout(form)
        for title, callback in [("선택 FDI 치아 기본 형태 등록", self.create_library_tooth),
                                ("외부 치아 STL 등록", lambda: self.import_mesh(role_override="library")),
                                ("폰틱 위치 선택", lambda: self.set_mode("placement")),
                                ("선택 치아 폰틱 생성", self.generate_pontic)]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            box.addWidget(button)
        note = QtWidgets.QLabel("외부 라이브러리는 +Z를 교합면으로 정렬한 모델을 사용하세요. 적합 후 두께와 접촉을 검사하세요.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        box.addWidget(note)
        note = QtWidgets.QLabel("지대치 표면에서 내면을 계산합니다. 생성 후 두께·교합·마진을 직접 검사하세요.")
        note.setWordWrap(True)
        note.setObjectName("hint")
        box.addWidget(note)

        box = self.page_layouts[4]
        note = QtWidgets.QLabel("선택 치아의 보철 표면을 드래그합니다. 내면과 마진 가장자리는 보호됩니다.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        box.addWidget(note)
        self.brush_group = QtWidgets.QButtonGroup(self)
        self.brush_buttons = {}
        for mode, title in [("add", "표면 추가"), ("remove", "표면 삭제"), ("smooth", "국소 부드럽게"), ("orbit", "보기 / 회전")]:
            button = QtWidgets.QPushButton(title)
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, m=mode: self.set_mode(m))
            self.brush_group.addButton(button)
            self.brush_buttons[mode] = button
            box.addWidget(button)
        form = QtWidgets.QFormLayout()
        self.brush_radius = QtWidgets.QDoubleSpinBox()
        self.brush_radius.setRange(0.3, 8)
        self.brush_radius.setValue(1.7)
        self.brush_radius.setSuffix(" mm")
        self.brush_strength = QtWidgets.QDoubleSpinBox()
        self.brush_strength.setRange(0.01, 0.25)
        self.brush_strength.setValue(0.06)
        self.brush_strength.setSingleStep(0.01)
        self.brush_strength.setSuffix(" mm / 획")
        form.addRow("브러시 반경", self.brush_radius)
        form.addRow("브러시 강도", self.brush_strength)
        box.addLayout(form)
        self.clearance = QtWidgets.QDoubleSpinBox()
        self.clearance.setRange(0, 1)
        self.clearance.setValue(0.05)
        self.clearance.setSuffix(" mm · 대합치 여유")
        box.addWidget(self.clearance)
        button = QtWidgets.QPushButton("대합치 방향으로 교합 조정")
        button.clicked.connect(self.adapt_opposition)
        box.addWidget(button)
        label = QtWidgets.QLabel("브릿지 연결")
        label.setObjectName("sectionTitle")
        box.addWidget(label)
        note = QtWidgets.QLabel("생성한 두 보철을 연결합니다. 출력 단계에서 교차부를 합친 단일 메시로 변환합니다.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        box.addWidget(note)
        self.bridge_a = QtWidgets.QComboBox()
        self.bridge_b = QtWidgets.QComboBox()
        box.addWidget(self.bridge_a)
        box.addWidget(self.bridge_b)
        self.connector_radius = QtWidgets.QDoubleSpinBox()
        self.connector_radius.setRange(0.6, 3.5)
        self.connector_radius.setValue(1.5)
        self.connector_radius.setSuffix(" mm · 연결부 반경")
        box.addWidget(self.connector_radius)
        button = QtWidgets.QPushButton("연결부 만들기")
        button.clicked.connect(self.build_bridge)
        box.addWidget(button)
        label = QtWidgets.QLabel("다점 배치 · 프레임워크")
        label.setObjectName("sectionTitle")
        box.addWidget(label)
        for title, callback in [("배치 경로 점 선택", lambda: self.set_mode("path")),
                                ("경로 점 초기화", self.clear_layout_path),
                                ("경로를 따라 치아 배열", self.arrange_teeth),
                                ("경로를 따라 연결 바 생성", self.build_path_bar)]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            box.addWidget(button)
        self.setup_count = QtWidgets.QSpinBox()
        self.setup_count.setRange(1, 16)
        self.setup_count.setValue(8)
        self.setup_count.setSuffix(" 개 · 배치할 치아")
        box.addWidget(self.setup_count)

        box = self.page_layouts[5]
        self.inspection_info = QtWidgets.QLabel("보철을 선택하고 검사 항목을 실행하세요.")
        self.inspection_info.setWordWrap(True)
        self.inspection_info.setObjectName("metric")
        self.inspection_info.setStyleSheet("font-size: 12px; font-weight: 400; padding: 10px;")
        self.inspection_info.setMinimumHeight(148)
        self.inspection_info.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Minimum)
        box.addWidget(self.inspection_info)
        for title, kind in [("실제 벽두께 거리맵", "thickness"), ("대합치 접촉 거리맵", "contact"), ("색상 초기화", None)]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda checked=False, k=kind: self.show_distances(k))
            box.addWidget(button)
        button = QtWidgets.QPushButton("메시 폐합 / 체적 검사")
        button.clicked.connect(self.inspect_mesh)
        box.addWidget(button)
        self.output_selfcheck = QtWidgets.QCheckBox("내보낼 때 삼각형 내부 교차 검사")
        self.output_selfcheck.setChecked(True)
        self.output_selfcheck.setToolTip("큰 모델은 검사에 시간이 걸릴 수 있습니다. 구조 검사와 별도로 삼각형 내부의 교차를 확인합니다.")
        box.addWidget(self.output_selfcheck)
        button = QtWidgets.QPushButton("두 점 거리 측정")
        button.clicked.connect(lambda: self.set_mode("measure"))
        box.addWidget(button)
        button = QtWidgets.QPushButton("측정 지우기")
        button.clicked.connect(self.measure_reset)
        box.addWidget(button)
        self.section_toggle = QtWidgets.QCheckBox("단면 보기")
        self.section_toggle.toggled.connect(self.toggle_section)
        box.addWidget(self.section_toggle)
        self.section_axis = QtWidgets.QComboBox()
        self.section_axis.addItems(["X 단면", "Y 단면", "Z 단면"])
        self.section_axis.currentIndexChanged.connect(self.section_changed)
        box.addWidget(self.section_axis)
        self.section_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.section_slider.setRange(0, 1000)
        self.section_slider.setValue(500)
        self.section_slider.valueChanged.connect(self.section_changed)
        box.addWidget(self.section_slider)
        self.wire_toggle = QtWidgets.QCheckBox("와이어프레임")
        self.wire_toggle.toggled.connect(self.toggle_wire)
        box.addWidget(self.wire_toggle)
        self.merge_export = QtWidgets.QCheckBox("브릿지 합집합 후 출력")
        self.merge_export.setChecked(True)
        self.merge_export.setToolTip("보철과 연결부를 결합하고 크라운 내부 공간을 보존합니다.")
        box.addWidget(self.merge_export)
        button = QtWidgets.QPushButton("제작물 내보내기")
        button.setObjectName("primary")
        button.clicked.connect(self.export_mesh)
        box.addWidget(button)
        button = QtWidgets.QPushButton("선택 모델 내보내기")
        button.clicked.connect(lambda: self.export_mesh(selected=True))
        box.addWidget(button)

        box = self.page_layouts[6]
        label = QtWidgets.QLabel("인쇄용 모델과 분리 다이")
        label.setObjectName("sectionTitle")
        box.addWidget(label)
        form = QtWidgets.QFormLayout()
        self.model_height = QtWidgets.QDoubleSpinBox()
        self.model_height.setRange(3, 30)
        self.model_height.setValue(8)
        self.model_height.setSuffix(" mm")
        self.model_trim_plane = QtWidgets.QDoubleSpinBox()
        self.model_trim_plane.setRange(-1000, 1000)
        self.model_trim_plane.setValue(0)
        self.model_trim_plane.setSuffix(" mm · Z")
        self.model_wall = QtWidgets.QDoubleSpinBox()
        self.model_wall.setRange(1, 5)
        self.model_wall.setValue(2)
        self.model_wall.setSuffix(" mm")
        self.model_drain = QtWidgets.QDoubleSpinBox()
        self.model_drain.setRange(1, 8)
        self.model_drain.setValue(3)
        self.model_drain.setSuffix(" mm")
        self.die_offset = QtWidgets.QDoubleSpinBox()
        self.die_offset.setRange(.1, 2)
        self.die_offset.setValue(.5)
        self.die_offset.setSuffix(" mm")
        for title, control in [("바닥 두께", self.model_height), ("절단 높이", self.model_trim_plane),
                               ("베이스 벽두께", self.model_wall), ("배수구 지름", self.model_drain), ("다이 외곽 여유", self.die_offset)]:
            form.addRow(title, control)
        box.addLayout(form)
        self.model_hollow = QtWidgets.QCheckBox("베이스 중공화 및 배수구")
        box.addWidget(self.model_hollow)
        for title, callback in [("선택 스캔에서 모델 생성", self.build_print_model), ("마진 주변 다이 분리", self.extract_die)]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            box.addWidget(button)
        label = QtWidgets.QLabel("스플린트 · 개인 트레이 기본 쉘")
        label.setObjectName("sectionTitle")
        box.addWidget(label)
        self.shell_kind = QtWidgets.QComboBox()
        self.shell_kind.addItems(["스플린트 기본 쉘", "개인 트레이 기본 쉘"])
        box.addWidget(self.shell_kind)
        form = QtWidgets.QFormLayout()
        self.shell_thickness = QtWidgets.QDoubleSpinBox()
        self.shell_thickness.setRange(.8, 5)
        self.shell_thickness.setValue(2)
        self.shell_thickness.setSuffix(" mm")
        self.shell_gap = QtWidgets.QDoubleSpinBox()
        self.shell_gap.setRange(0, 3)
        self.shell_gap.setValue(.2)
        self.shell_gap.setSuffix(" mm")
        self.shell_trim = QtWidgets.QDoubleSpinBox()
        self.shell_trim.setRange(-1000, 1000)
        self.shell_trim.setValue(0)
        self.shell_trim.setSuffix(" mm · Z")
        for title, control in [("쉘 두께", self.shell_thickness), ("스캔 여유", self.shell_gap), ("하단 절단 높이", self.shell_trim)]:
            form.addRow(title, control)
        box.addLayout(form)
        self.shell_handle = QtWidgets.QCheckBox("트레이 손잡이 추가")
        box.addWidget(self.shell_handle)
        button = QtWidgets.QPushButton("선택 모델에서 쉘 생성")
        button.setObjectName("primary")
        button.clicked.connect(self.build_shell)
        box.addWidget(button)
        note = QtWidgets.QLabel("기본 쉘과 배열·바는 설계 시작 형상입니다. 장치별 변연·교합·유지부와 적합성은 별도 설계·검증이 필요합니다.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        box.addWidget(note)

        box = self.page_layouts[7]
        self.selected_printer_label = QtWidgets.QLabel("선택한 프린터 없음")
        self.selected_printer_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.selected_printer_label.setWordWrap(True)
        box.addWidget(self.selected_printer_label)
        button = QtWidgets.QPushButton("프린터 검색 · 선택")
        button.clicked.connect(self.show_printer_dialog)
        box.addWidget(button)
        self.refresh_printer_controls()
        self.output_scope = QtWidgets.QComboBox()
        self.output_scope.addItems(["전체 제작물", "선택 오브젝트", "치아별 / 브릿지별 파일"])
        box.addWidget(self.output_scope)
        self.output_format = QtWidgets.QComboBox()
        self.output_format.addItems(["STL · 바이너리", "3MF · mm 단위 / 다중 파트"])
        box.addWidget(self.output_format)
        form = QtWidgets.QFormLayout()
        self.print_rx = QtWidgets.QDoubleSpinBox()
        self.print_ry = QtWidgets.QDoubleSpinBox()
        self.print_rz = QtWidgets.QDoubleSpinBox()
        for title, control in [("회전 X", self.print_rx), ("회전 Y", self.print_ry), ("회전 Z", self.print_rz)]:
            control.setRange(-180, 180)
            control.setSuffix("°")
            form.addRow(title, control)
        self.print_spacing = QtWidgets.QDoubleSpinBox()
        self.print_spacing.setRange(1, 30)
        self.print_spacing.setValue(5)
        self.print_spacing.setSuffix(" mm")
        form.addRow("파트 간격", self.print_spacing)
        self.printer_bed_x = QtWidgets.QDoubleSpinBox()
        self.printer_bed_y = QtWidgets.QDoubleSpinBox()
        self.printer_bed_z = QtWidgets.QDoubleSpinBox()
        for title, control, initial in [("출력판 X", self.printer_bed_x, 140), ("출력판 Y", self.printer_bed_y, 80), ("출력 높이", self.printer_bed_z, 160)]:
            control.setRange(10, 2000)
            control.setValue(initial)
            control.setSuffix(" mm")
            form.addRow(title, control)
        for control in [self.printer_bed_x, self.printer_bed_y, self.printer_bed_z]:
            control.valueChanged.connect(self.update_print_bed)
        box.addLayout(form)
        for title, callback in [("원본을 보존하고 출력판에 배치", self.prepare_print_layout),
                                ("검사 후 STL / 3MF 내보내기", self.export_mesh)]:
            button = QtWidgets.QPushButton(title)
            button.setObjectName("primary")
            button.clicked.connect(callback)
            box.addWidget(button)
        label = QtWidgets.QLabel("슬라이서 연결")
        label.setObjectName("sectionTitle")
        box.addWidget(label)
        self.slicer_path = QtWidgets.QLineEdit()
        self.slicer_path.setPlaceholderText("PrusaSlicer / OrcaSlicer / CHITUBOX 등 EXE")
        self.slicer_path.setText(str(QtCore.QSettings("EXOCADE", "DentalDesign").value("slicer_path", "")))
        box.addWidget(self.slicer_path)
        self.auto_open_slicer = QtWidgets.QCheckBox("내보낸 뒤 지정 슬라이서 열기")
        box.addWidget(self.auto_open_slicer)
        self.print_info = QtWidgets.QLabel("출력 준비와 내보내기 결과가 여기에 표시됩니다.")
        self.print_info.setObjectName("hint")
        self.print_info.setWordWrap(True)
        box.addWidget(self.print_info)
        for title, callback in [("설치 슬라이서 자동 찾기", self.find_slicer), ("슬라이서 EXE 직접 지정", self.choose_slicer),
                                ("최근 출력물을 슬라이서에서 열기", self.open_slicer)]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            box.addWidget(button)
        note = QtWidgets.QLabel("STL은 mm로 저장합니다. 3MF는 단위를 포함합니다. 지지대·레진 노광·적층 두께·장비 전용 파일은 프린터 프로필을 선택한 슬라이서에서 설정하세요.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        box.addWidget(note)
        note = QtWidgets.QLabel("출력 준비 사본이 있으면 전체 내보내기에 우선 사용합니다. 원본을 수정한 뒤에는 다시 배치하세요.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        box.addWidget(note)

        box = self.page_layouts[8]
        button = QtWidgets.QPushButton("DICOM 시리즈 폴더 불러오기")
        button.setObjectName("primary")
        button.clicked.connect(self.import_dicom)
        box.addWidget(button)
        self.ct_info = QtWidgets.QLabel("CT / CBCT 원본 단면과 임계값 기반 표면 생성")
        self.ct_info.setObjectName("hint")
        self.ct_info.setWordWrap(True)
        box.addWidget(self.ct_info)
        self.ct_preview = QtWidgets.QLabel()
        self.ct_preview.setMinimumHeight(180)
        self.ct_preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        box.addWidget(self.ct_preview)
        self.ct_slice = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.ct_slice.setRange(0, 0)
        self.ct_slice.valueChanged.connect(self.update_ct_preview)
        box.addWidget(self.ct_slice)
        self.ct_threshold = QtWidgets.QDoubleSpinBox()
        self.ct_threshold.setRange(-32768, 32767)
        self.ct_threshold.setValue(500)
        self.ct_threshold.setSuffix(" · 임계 강도")
        box.addWidget(self.ct_threshold)
        self.ct_largest = QtWidgets.QCheckBox("가장 큰 연결 영역만 유지")
        box.addWidget(self.ct_largest)
        button = QtWidgets.QPushButton("분할 표면 생성")
        button.clicked.connect(self.build_dicom_surface)
        box.addWidget(button)
        note = QtWidgets.QLabel("분할은 임계 강도 기반입니다. 장비별 값과 영상 경계를 확인하세요. 원본 DICOM은 프로젝트에 저장하지 않으며 생성한 표면을 보관합니다.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        box.addWidget(note)
        for box in self.page_layouts:
            box.addStretch()
        bottom_buttons = QtWidgets.QHBoxLayout()
        back = QtWidgets.QPushButton("이전")
        back.clicked.connect(lambda: self.set_step(max(0, self.pages.currentIndex() - 1)))
        next_button = QtWidgets.QPushButton("다음 단계  →")
        next_button.setObjectName("primary")
        next_button.clicked.connect(lambda: self.set_step(min(self.pages.count()-1, self.pages.currentIndex() + 1)))
        bottom_buttons.addWidget(back)
        bottom_buttons.addWidget(next_button)
        right_layout.addLayout(bottom_buttons)
        splitter.addWidget(right_frame)
        splitter.setSizes([220, 980, 315])
        splitter.setStretchFactor(1, 1)
        self.statusBar().showMessage("준비")
        disclaimer = QtWidgets.QLabel("mm  |  연구·설계용 프로토타입 · 임상 검증 전  ")
        disclaimer.setObjectName("hint")
        self.statusBar().addPermanentWidget(disclaimer)
        for shortcut, callback in [("Ctrl+Z", self.undo), ("Ctrl+Y", self.redo), ("F", lambda: self.camera_view("fit")),
                                   ("Escape", lambda: self.set_mode("orbit")), ("Ctrl+Shift+S", lambda: self.save_project(save_as=True))]:
            action = QtGui.QAction(self)
            action.setShortcut(shortcut)
            action.triggered.connect(callback)
            self.addAction(action)
        self.vtk_widget.Initialize()
        self.orientation.SetEnabled(1)
        self.orientation.InteractiveOff()
        self.select_tooth(16)
        self.set_step(0)
        startup_project = next((Path(value) for value in sys.argv[1:] if Path(value).suffix.lower() == ".exo" and Path(value).is_file()), None)
        if startup_project:
            QtCore.QTimer.singleShot(150, lambda: self.open_project(path_override=str(startup_project)))
        else:
            QtCore.QTimer.singleShot(150, self.load_demo)

    def set_step(self, index):
        self.pages.setCurrentIndex(index)
        self.step_buttons[index].setChecked(True)
        self.panel_title.setText(["스캔 데이터", "마진선 지정", "삽입축 설정", "보철 설계", "형태 편집", "검사 및 출력", "모델 · 장치 생성", "출력 준비 · STL / 3MF", "DICOM / CT 표면"][index])
        self.set_mode("orbit")

    def select_tooth(self, tooth):
        self.current_tooth = int(tooth)
        self.teeth.setdefault(str(tooth), {"margin": [], "closed": False, "axis": [0, 0, 1]})
        for number, button in self.tooth_buttons.items():
            button.setChecked(number == tooth)
        self.tooth_label.setText(f"선택 치아  ·  {tooth}")
        axis = self.teeth[str(tooth)]["axis"]
        self.axis_info.setText(f"X {axis[0]:.3f}   Y {axis[1]:.3f}   Z {axis[2]:.3f}")
        for key, obj in self.objects.items():
            if obj.get("tooth") == tooth and obj["role"] in ["restoration", "pontic"]:
                self.active_id = key
                for i in range(self.object_list.topLevelItemCount()):
                    item = self.object_list.topLevelItem(i)
                    if item.data(0, QtCore.Qt.ItemDataRole.UserRole) == key:
                        self.object_list.setCurrentItem(item)
                        break
                params = obj.get("params", {})
                self.restoration_type.setCurrentIndex(params.get("type", 0))
                for name, control in self.design_controls.items():
                    if name in params:
                        control.setValue(params[name])
                self.shape_type.setCurrentIndex(params.get("shape", 0))
                library_index = self.library_combo.findData(params.get("library_id"))
                self.library_combo.setCurrentIndex(max(0, library_index))
                self.library_scale.setValue(params.get("library_scale", 1))
                self.library_rotation.setValue(params.get("library_rotation", 0))
                break
        self.update_margin()

    def add_object(self, mesh, name, role, key=None, **metadata):
        if mesh.GetNumberOfPoints() == 0 or mesh.GetNumberOfPolys() == 0:
            raise ValueError("비어 있는 표면 메시입니다.")
        key = key or uuid.uuid4().hex
        normals = vtk.vtkPolyDataNormals()
        normals.SetInputData(mesh)
        normals.SplittingOff()
        normals.ConsistencyOn()
        normals.AutoOrientNormalsOff()
        normals.Update()
        data = vtk.vtkPolyData()
        data.DeepCopy(normals.GetOutput())
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(data)
        mapper.ScalarVisibilityOff()
        if self.section_enabled:
            mapper.AddClippingPlane(self.section_plane)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        colors = {"scan": (0.74, 0.69, 0.59), "antagonist": (0.43, 0.62, 0.72),
                  "reference": (0.59, 0.63, 0.72), "restoration": (0.97, 0.85, 0.60), "connector": (0.94, 0.68, 0.33),
                  "model": (.65, .76, .85), "die": (.74, .84, .89), "appliance": (.48, .80, .76),
                  "library": (.86, .84, .75), "part": (.6, .64, .71), "pontic": (.95, .81, .54),
                  "bar": (.77, .80, .87), "print": (.96, .71, .37), "segmentation": (.87, .81, .70)}
        color = metadata.pop("color", colors.get(role, (0.8, 0.8, 0.8)))
        actor.GetProperty().SetColor(*color)
        actor.GetProperty().SetSpecular(0.24)
        actor.GetProperty().SetSpecularPower(35)
        actor.GetProperty().SetInterpolationToPhong()
        actor.GetProperty().SetOpacity(metadata.pop("opacity", 0.40 if role == "antagonist" else 1.0))
        actor.SetVisibility(metadata.pop("visible", True))
        if self.wire_toggle.isChecked():
            actor.GetProperty().SetRepresentationToWireframe()
        self.objects[key] = {"mesh": data, "actor": actor, "name": name, "role": role, "color": color, **metadata}
        self.renderer.AddActor(actor)
        self.active_id = key
        self.refresh_scene()
        self.dirty = True
        return key

    def refresh_scene(self):
        self.object_list.blockSignals(True)
        self.object_list.clear()
        symbols = {"scan": "스캔", "antagonist": "대합", "reference": "참고", "restoration": "보철", "connector": "연결",
                   "model": "모델", "die": "다이", "appliance": "장치", "library": "치아형", "part": "부품",
                   "pontic": "폰틱", "bar": "바", "print": "출력", "segmentation": "CT"}
        for key, obj in self.objects.items():
            item = QtWidgets.QTreeWidgetItem([f"{symbols.get(obj['role'], '')}  {obj['name']}"])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, key)
            item.setCheckState(0, QtCore.Qt.CheckState.Checked if obj["actor"].GetVisibility() else QtCore.Qt.CheckState.Unchecked)
            self.object_list.addTopLevelItem(item)
            if key == self.active_id:
                self.object_list.setCurrentItem(item)
        self.object_list.blockSignals(False)
        for combo in [self.bridge_a, self.bridge_b]:
            previous = combo.currentData()
            combo.clear()
            for key, obj in self.objects.items():
                if obj["role"] in ["restoration", "pontic"]:
                    combo.addItem(obj["name"], key)
            index = combo.findData(previous)
            if index >= 0:
                combo.setCurrentIndex(index)
        if self.bridge_b.count() > 1 and self.bridge_a.currentIndex() == self.bridge_b.currentIndex():
            self.bridge_b.setCurrentIndex(1)
        previous_library = self.library_combo.currentData()
        self.library_combo.clear()
        self.library_combo.addItem("매개변수 기본 형태", None)
        for key, obj in self.objects.items():
            if obj["role"] == "library":
                self.library_combo.addItem(obj["name"], key)
        index = self.library_combo.findData(previous_library)
        self.library_combo.setCurrentIndex(max(0, index))
        for tooth, button in self.tooth_buttons.items():
            exists = any(o.get("tooth") == tooth for o in self.objects.values() if o["role"] in ["restoration", "pontic"])
            button.setText(f"{tooth}•" if exists else str(tooth))
        self.selection_changed(self.object_list.currentItem(), None)
        self.renderer.ResetCameraClippingRange()
        self.vtk_widget.GetRenderWindow().Render()

    def selection_changed(self, current, previous):
        if current is None:
            self.object_info.setText("오브젝트를 선택하세요.")
            return
        key = current.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if key not in self.objects:
            return
        self.active_id = key
        obj = self.objects[key]
        self.opacity.blockSignals(True)
        self.opacity.setValue(round(obj["actor"].GetProperty().GetOpacity() * 100))
        self.opacity.blockSignals(False)
        bounds = obj["mesh"].GetBounds()
        self.object_info.setText(f"{obj['mesh'].GetNumberOfPoints():,} 정점 · {obj['mesh'].GetNumberOfPolys():,} 면\n"
                                 f"{bounds[1]-bounds[0]:.1f} × {bounds[3]-bounds[2]:.1f} × {bounds[5]-bounds[4]:.1f} mm")

    def visibility_changed(self, item, column):
        key = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if key in self.objects:
            self.objects[key]["actor"].SetVisibility(item.checkState(0) == QtCore.Qt.CheckState.Checked)
            self.dirty = True
            self.vtk_widget.GetRenderWindow().Render()

    def opacity_changed(self, value):
        if self.active_id in self.objects:
            self.objects[self.active_id]["actor"].GetProperty().SetOpacity(value / 100)
            self.dirty = True
            self.vtk_widget.GetRenderWindow().Render()

    def import_mesh(self, checked=False, role_override=None):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "스캔 파일 불러오기", "", "3D 모델 (*.stl *.ply *.obj *.vtp)")
        if not paths:
            return
        role = role_override or self.import_role.currentData()
        self.push_undo()
        for path in paths:
            try:
                extension = Path(path).suffix.lower()
                reader = {".stl": vtk.vtkSTLReader, ".ply": vtk.vtkPLYReader, ".obj": vtk.vtkOBJReader, ".vtp": vtk.vtkXMLPolyDataReader}[extension]()
                reader.SetFileName(path)
                reader.Update()
                triangulate = vtk.vtkTriangleFilter()
                triangulate.SetInputData(reader.GetOutput())
                triangulate.Update()
                clean = vtk.vtkCleanPolyData()
                clean.SetInputConnection(triangulate.GetOutputPort())
                clean.Update()
                mesh = clean.GetOutput()
                if mesh.GetNumberOfPolys() == 0 or not np.isfinite(vtk_to_numpy(mesh.GetPoints().GetData())).all():
                    raise ValueError("유효한 삼각형 표면을 읽을 수 없습니다.")
                key = self.add_object(mesh, Path(path).stem, role, source=Path(path).name, visible=role != "library")
                if role == "library":
                    self.library_combo.setCurrentIndex(self.library_combo.findData(key))
                self.statusBar().showMessage(f"불러오기 완료 · {Path(path).name}", 8000)
            except Exception as error:
                QtWidgets.QMessageBox.warning(self, "불러오기 실패", str(error))
        self.camera_view("iso")

    def new_case(self):
        if self.dirty:
            result = QtWidgets.QMessageBox.question(self, "케이스 변경", "현재 변경사항을 저장하시겠습니까?",
                QtWidgets.QMessageBox.StandardButton.Save | QtWidgets.QMessageBox.StandardButton.Discard | QtWidgets.QMessageBox.StandardButton.Cancel)
            if result == QtWidgets.QMessageBox.StandardButton.Cancel:
                return False
            if result == QtWidgets.QMessageBox.StandardButton.Save and not self.save_project():
                return False
        self.clear_scene()
        self.section_toggle.setChecked(False)
        self.section_slider.setValue(500)
        self.wire_toggle.setChecked(False)
        self.case_name.setText("새 케이스")
        self.project_path = None
        self.teeth = {}
        self.demo_margins = {}
        self.layout_points = []
        if self.path_actor:
            self.renderer.RemoveActor(self.path_actor)
            self.path_actor = None
        if self.bed_actor:
            self.renderer.RemoveActor(self.bed_actor)
            self.bed_actor = None
        self.ct_volume = None
        self.ct_metadata = None
        self.ct_info.setText("CT / CBCT 원본 단면과 임계값 기반 표면 생성")
        self.ct_preview.clear()
        self.last_export_paths = []
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.select_tooth(16)
        self.set_step(0)
        self.dirty = False
        return True

    def clear_scene(self):
        for obj in self.objects.values():
            self.renderer.RemoveActor(obj["actor"])
        self.objects.clear()
        self.active_id = None
        if self.path_actor:
            self.renderer.RemoveActor(self.path_actor)
            self.path_actor = None
        if self.bed_actor:
            self.renderer.RemoveActor(self.bed_actor)
            self.bed_actor = None
        self.color_mode = None
        if self.scalar_bar:
            self.renderer.RemoveViewProp(self.scalar_bar)
            self.scalar_bar = None
        self.measure_reset()
        if self.margin_actor:
            self.renderer.RemoveActor(self.margin_actor)
            self.margin_actor = None
        self.refresh_scene()

    def load_demo(self):
        if not self.new_case():
            return
        self.clear_scene()
        self.teeth = {}
        self.demo_margins = {}
        jaw = vtk.vtkAppendPolyData()
        opponent = vtk.vtkAppendPolyData()
        tooth_numbers = [17, 16, 15, 14, 13, 12, 11, 21, 22, 23, 24, 25, 26, 27]
        gum_points = vtk.vtkPoints()
        for index, (tooth, angle) in enumerate(zip(tooth_numbers, np.linspace(-1.35, 1.35, 14))):
            x, y = 30 * math.sin(angle), 29 * math.cos(angle) - 14
            gum_points.InsertNextPoint(x, y, -1.5)
            posterior = tooth % 10 >= 4
            rx, ry = (3.6, 4.2) if tooth % 10 >= 6 else ((3.0, 3.4) if posterior else (2.6, 2.4))
            if tooth == 16:
                count = 96
                points = [[x, y, -1.2]]
                for z, radius in [(-1.2, 3.15), (0.7, 3.35), (1.0, 3.35), (1.2, 3.1), (4.5, 2.35), (4.8, 2.1)]:
                    for theta in np.linspace(0, 2 * np.pi, count, endpoint=False):
                        points.append([x + radius * math.cos(theta), y + radius * 1.12 * math.sin(theta), z])
                points.append([x, y, 4.85])
                faces = []
                for j in range(count):
                    k = (j + 1) % count
                    faces.append([0, 1+k, 1+j])
                    for ring in range(5):
                        a, b = 1+ring*count+j, 1+ring*count+k
                        c, d = a+count, b+count
                        faces.extend([[a, b, d], [a, d, c]])
                    faces.append([len(points)-1, 1+5*count+j, 1+5*count+k])
                mesh = vtk.vtkPolyData()
                vtk_points = vtk.vtkPoints()
                vtk_points.SetData(numpy_to_vtk(np.asarray(points, dtype=np.float64), deep=True))
                mesh.SetPoints(vtk_points)
                cells = vtk.vtkCellArray()
                packed = np.column_stack([np.full(len(faces), 3), np.asarray(faces)]).astype(np.int64).ravel()
                cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(packed, deep=True))
                mesh.SetPolys(cells)
                jaw.AddInputData(mesh)
                self.demo_margins[tooth] = [[x+3.35*math.cos(t), y+3.35*1.12*math.sin(t), 1.0] for t in np.linspace(0, 2*np.pi, 32, endpoint=False)]
            else:
                source = vtk.vtkSuperquadricSource()
                source.SetCenter(x, y, 2.4)
                source.SetScale(rx, ry, 3.8 if posterior else 4.2)
                source.SetSize(1)
                source.SetPhiRoundness(0.55)
                source.SetThetaRoundness(0.65)
                source.SetThetaResolution(40)
                source.SetPhiResolution(32)
                source.Update()
                surface = vtk.vtkPolyData()
                surface.DeepCopy(source.GetOutput())
                coords = vtk_to_numpy(surface.GetPoints().GetData())
                u, v = (coords[:, 0]-x)/rx, (coords[:, 1]-y)/ry
                upper = np.clip((coords[:, 2]-3.8)/2, 0, 1)
                relief = (0.6*np.cos(u*np.pi*1.5)*np.cos(v*np.pi*1.5) if posterior else 0.2*np.cos(v*np.pi))
                coords[:, 2] += upper * relief
                surface.GetPoints().Modified()
                jaw.AddInputData(surface)
            source = vtk.vtkSuperquadricSource()
            source.SetCenter(x, y+0.5, 11.5)
            source.SetScale(rx*0.97, ry*0.97, 3.0)
            source.SetSize(1)
            source.SetPhiRoundness(0.6)
            source.SetThetaRoundness(0.65)
            source.SetThetaResolution(32)
            source.SetPhiResolution(24)
            source.Update()
            opponent.AddInputData(source.GetOutput())
        spline = vtk.vtkParametricSpline()
        spline.SetPoints(gum_points)
        sample = vtk.vtkParametricFunctionSource()
        sample.SetParametricFunction(spline)
        sample.SetUResolution(180)
        tube = vtk.vtkTubeFilter()
        tube.SetInputConnection(sample.GetOutputPort())
        tube.SetRadius(4.25)
        tube.SetNumberOfSides(24)
        tube.CappingOn()
        tube.Update()
        jaw.AddInputData(tube.GetOutput())
        for pipeline, name, role in [(jaw, "예제 작업악 · 16번 지대치", "scan"), (opponent, "예제 대합치", "antagonist")]:
            pipeline.Update()
            tri = vtk.vtkTriangleFilter()
            tri.SetInputConnection(pipeline.GetOutputPort())
            tri.Update()
            clean = vtk.vtkCleanPolyData()
            clean.SetInputConnection(tri.GetOutputPort())
            clean.Update()
            self.add_object(clean.GetOutput(), name, role, demo=True)
        self.active_id = next(k for k, o in self.objects.items() if o["role"] == "scan")
        self.case_name.setText("예제 · 단일 크라운 16")
        self.project_path = None
        self.select_tooth(16)
        self.refresh_scene()
        self.camera_view("iso")
        self.dirty = False
        self.statusBar().showMessage("예제 준비 완료 · 02 마진선에서 예제 16번 마진을 사용할 수 있습니다.")

    def set_mode(self, mode):
        self.mode = mode
        self.dragging = False
        for key, button in self.brush_buttons.items():
            button.setChecked(key == mode)
        titles = {"orbit": "보기 모드", "margin": "마진 점 선택", "seed": "단면 기준점 선택", "measure": "두 점 측정",
                  "add": "표면 추가", "remove": "표면 삭제", "smooth": "표면 부드럽게", "path": "배치 경로 선택", "placement": "폰틱 위치 선택"}
        self.mode_label.setText(titles.get(mode, mode))
        self.vtk_widget.setCursor(QtCore.Qt.CursorShape.ArrowCursor if mode == "orbit" else QtCore.Qt.CursorShape.CrossCursor)

    def mouse_press(self, caller, event):
        if self.mode == "orbit":
            self.style.OnLeftButtonDown()
            return
        x, y = self.interactor.GetEventPosition()
        if not self.picker.Pick(x, y, 0, self.renderer):
            return
        actor = self.picker.GetActor()
        picked = next((key for key, obj in self.objects.items() if obj["actor"] == actor), None)
        if picked is None:
            return
        point = list(self.picker.GetPickPosition())
        if self.mode in ["path", "placement"]:
            self.push_undo()
            if self.mode == "path":
                if not self.layout_points or np.linalg.norm(np.asarray(point)-self.layout_points[-1]) > .2:
                    self.layout_points.append(point)
                    self.update_layout_path()
                self.statusBar().showMessage(f"배치 경로 · {len(self.layout_points)}개 점 선택", 5000)
            else:
                self.teeth[str(self.current_tooth)]["placement"] = point
                self.statusBar().showMessage(f"{self.current_tooth}번 폰틱의 경부 위치를 선택했습니다.", 5000)
                self.set_mode("orbit")
            self.dirty = True
        elif self.mode in ["margin", "seed"]:
            if self.objects[picked]["role"] != "scan":
                self.statusBar().showMessage("작업악 스캔 표면을 클릭하세요. 대합치는 숨길 수 있습니다.", 5000)
                return
            self.push_undo()
            state = self.teeth[str(self.current_tooth)]
            state["scan_id"] = picked
            if self.mode == "seed":
                state["seed"] = point
                self.statusBar().showMessage("단면 기준점을 선택했습니다. 윤곽 추출을 실행하세요.", 5000)
                self.set_mode("orbit")
            else:
                state["margin"].append(point)
                state["closed"] = False
                self.update_margin()
            self.dirty = True
        elif self.mode == "measure":
            self.measure_points.append(point)
            if len(self.measure_points) == 2:
                line = vtk.vtkLineSource()
                line.SetPoint1(self.measure_points[0])
                line.SetPoint2(self.measure_points[1])
                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputConnection(line.GetOutputPort())
                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                actor.GetProperty().SetColor(0.3, 0.95, 0.8)
                actor.GetProperty().SetLineWidth(3)
                actor.PickableOff()
                self.renderer.AddActor(actor)
                self.measure_actors.append(actor)
                distance = np.linalg.norm(np.array(self.measure_points[1])-self.measure_points[0])
                text = vtk.vtkBillboardTextActor3D()
                text.SetInput(f"{distance:.3f} mm")
                text.SetPosition(*np.mean(self.measure_points, axis=0))
                text.GetTextProperty().SetFontSize(17)
                text.GetTextProperty().SetColor(0.4, 1, 0.8)
                text.PickableOff()
                self.renderer.AddActor(text)
                self.measure_actors.append(text)
                self.inspection_info.setText(f"두 점 거리  {distance:.3f} mm")
                self.statusBar().showMessage(f"측정 거리 · {distance:.3f} mm", 10000)
                self.measure_points = []
                self.vtk_widget.GetRenderWindow().Render()
        elif self.mode in ["add", "remove", "smooth"]:
            if self.objects[picked]["role"] not in ["restoration", "pontic"] or self.objects[picked].get("tooth") != self.current_tooth:
                self.statusBar().showMessage("선택 치아의 보철 표면을 클릭하세요.", 4000)
                return
            self.active_id = picked
            self.push_undo()
            self.dragging = True
            self.last_brush_time = 0
            self.apply_brush(point)

    def mouse_move(self, caller, event):
        if self.mode == "orbit" or self.style.GetState() != 0:
            self.style.OnMouseMove()
            return
        if self.dragging and time.monotonic()-self.last_brush_time > 0.025:
            x, y = self.interactor.GetEventPosition()
            if self.picker.Pick(x, y, 0, self.renderer) and self.active_id in self.objects and self.picker.GetActor() == self.objects[self.active_id]["actor"]:
                self.apply_brush(self.picker.GetPickPosition())

    def mouse_release(self, caller, event):
        if self.mode == "orbit":
            self.style.OnLeftButtonUp()
        self.dragging = False

    def update_margin(self):
        if self.margin_actor:
            self.renderer.RemoveActor(self.margin_actor)
            self.margin_actor = None
        state = self.teeth.get(str(self.current_tooth), {})
        points = state.get("margin", [])
        self.margin_info.setText(f"{self.current_tooth}번 치아 · {len(points)}개 점 · " + ("폐곡선 완료" if state.get("closed") else "점 선택 후 마진을 닫으세요."))
        if points:
            data = vtk.vtkPolyData()
            vtk_points = vtk.vtkPoints()
            for point in points:
                vtk_points.InsertNextPoint(point)
            data.SetPoints(vtk_points)
            lines = vtk.vtkCellArray()
            ids = list(range(len(points))) + ([0] if state.get("closed") else [])
            lines.InsertNextCell(len(ids))
            for index in ids:
                lines.InsertCellPoint(index)
            data.SetLines(lines)
            verts = vtk.vtkCellArray()
            for index in range(len(points)):
                verts.InsertNextCell(1)
                verts.InsertCellPoint(index)
            data.SetVerts(verts)
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(data)
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(1, 0.50, 0.13)
            actor.GetProperty().SetLineWidth(3)
            actor.GetProperty().SetPointSize(7)
            actor.GetProperty().RenderPointsAsSpheresOn()
            actor.GetProperty().LightingOff()
            actor.PickableOff()
            self.renderer.AddActor(actor)
            self.margin_actor = actor
        self.vtk_widget.GetRenderWindow().Render()

    def margin_undo(self):
        state = self.teeth[str(self.current_tooth)]
        if state["margin"]:
            self.push_undo()
            state["margin"].pop()
            state["closed"] = False
            self.dirty = True
            self.update_margin()

    def margin_clear(self):
        self.push_undo()
        state = self.teeth[str(self.current_tooth)]
        state["margin"] = []
        state["closed"] = False
        self.dirty = True
        self.update_margin()

    def margin_close(self):
        state = self.teeth[str(self.current_tooth)]
        if len(state["margin"]) < 6:
            self.statusBar().showMessage("마진 점을 최소 6개 지정하세요.", 6000)
            return
        self.push_undo()
        state["closed"] = True
        self.dirty = True
        self.set_mode("orbit")
        self.update_margin()

    def demo_margin(self):
        scan_id = next((k for k, o in self.objects.items() if o["role"] == "scan" and o.get("demo")), None)
        if 16 not in self.demo_margins or scan_id is None:
            self.statusBar().showMessage("예제 케이스에서만 사용할 수 있습니다.", 5000)
            return
        self.select_tooth(16)
        self.push_undo()
        state = self.teeth["16"]
        state["margin"] = copy.deepcopy(self.demo_margins[16])
        state["closed"] = True
        state["scan_id"] = scan_id
        self.dirty = True
        self.update_margin()
        self.focus_selection()

    def margin_section(self):
        state = self.teeth[str(self.current_tooth)]
        if "seed" not in state or state.get("scan_id") not in self.objects:
            self.statusBar().showMessage("먼저 작업악의 마진 높이에 기준점을 선택하세요.", 6000)
            return
        plane = vtk.vtkPlane()
        plane.SetOrigin(state["seed"])
        plane.SetNormal(state["axis"])
        cutter = vtk.vtkCutter()
        cutter.SetCutFunction(plane)
        cutter.SetInputData(self.objects[state["scan_id"]]["mesh"])
        strip = vtk.vtkStripper()
        strip.SetInputConnection(cutter.GetOutputPort())
        strip.JoinContiguousSegmentsOn()
        strip.Update()
        data = strip.GetOutput()
        candidates = []
        for i in range(data.GetNumberOfCells()):
            cell = data.GetCell(i)
            if cell.GetNumberOfPoints() >= 8:
                points = np.array([data.GetPoint(cell.GetPointId(j)) for j in range(cell.GetNumberOfPoints())])
                if np.linalg.norm(points[0]-points[-1]) < 0.15:
                    candidates.append((np.min(np.linalg.norm(points-state["seed"], axis=1)), points[:-1]))
        if not candidates:
            self.statusBar().showMessage("닫힌 단면을 찾지 못했습니다. 수동으로 마진을 지정하세요.", 6000)
            return
        points = min(candidates, key=lambda item: item[0])[1]
        stride = max(1, len(points)//80)
        self.push_undo()
        state["margin"] = points[::stride].tolist()
        state["closed"] = False
        self.dirty = True
        self.update_margin()
        self.statusBar().showMessage("단면 윤곽을 추출했습니다. 해당 치아의 경계인지 확인한 후 마진을 닫으세요.", 10000)

    def set_axis(self, source):
        axis = np.array([0, 0, 1 if source == "z" else -1], dtype=float)
        if source == "camera":
            camera = self.renderer.GetActiveCamera()
            axis = np.array(camera.GetPosition())-camera.GetFocalPoint()
        axis /= np.linalg.norm(axis)
        self.push_undo()
        self.teeth[str(self.current_tooth)]["axis"] = axis.tolist()
        self.axis_info.setText(f"X {axis[0]:.3f}   Y {axis[1]:.3f}   Z {axis[2]:.3f}")
        self.dirty = True

    def material_changed(self, index):
        self.design_controls["wall"].setValue([0.8, 1.2, 1.0, 0.5][index])

    def restoration_changed(self, index):
        if not hasattr(self, "design_controls") or "height" not in self.design_controls:
            return
        self.design_controls["height"].setValue([1.4, 0, .15, .7, 0][index])
        self.design_controls["cusp"].setValue([.75, 0, .15, .4, 0][index])
        self.design_controls["width"].setValue(1.08 if index == 0 else 1)
        self.design_controls["edge"].setValue(.08 if index in [2, 3, 4] else .15)

    def suggest_axis(self):
        state = self.teeth[str(self.current_tooth)]
        source = self.objects.get(state.get("scan_id")) or self.objects.get(self.active_id)
        if source is None or len(state.get("margin", [])) < 6:
            self.statusBar().showMessage("먼저 작업악의 마진선을 지정하세요.", 6000)
            return
        mesh = source["mesh"]
        vertices = vtk_to_numpy(mesh.GetPoints().GetData())
        faces = vtk_to_numpy(mesh.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
        positions = vertices[faces].mean(axis=1)
        cross = np.cross(vertices[faces[:, 1]]-vertices[faces[:, 0]], vertices[faces[:, 2]]-vertices[faces[:, 0]])
        areas = np.linalg.norm(cross, axis=1)
        normals = cross/np.maximum(areas[:, None], 1e-12)
        margin = np.asarray(state["margin"])
        center = margin.mean(axis=0)
        axis = np.asarray(state["axis"], dtype=float)
        axis /= np.linalg.norm(axis)
        relative = positions-center
        vertical = relative @ axis
        radial = np.linalg.norm(relative-vertical[:, None]*axis, axis=1)
        radius = np.max(np.linalg.norm(margin-center, axis=1))
        basis = np.array([1., 0, 0]) if abs(axis[0]) < .8 else np.array([0., 1, 0])
        basis -= axis*np.dot(basis, axis)
        basis /= np.linalg.norm(basis)
        second = np.cross(axis, basis)
        margin_relative = margin-center
        margin_angles = np.arctan2(margin_relative @ second, margin_relative @ basis)
        angles = np.arctan2(relative @ second, relative @ basis)
        order = np.argsort(margin_angles)
        finish_height = np.interp(angles, margin_angles[order], (margin_relative @ axis)[order], period=2*np.pi)
        projected_x, projected_y = relative @ basis, relative @ second
        outline = np.column_stack([margin_relative @ basis, margin_relative @ second])
        inside = np.zeros(len(positions), dtype=bool)
        for index, a in enumerate(outline):
            b = outline[(index+1)%len(outline)]
            crossing = (a[1] > projected_y) != (b[1] > projected_y)
            denominator = b[1]-a[1]
            if abs(denominator) > 1e-12:
                inside ^= crossing & (projected_x < (b[0]-a[0])*(projected_y-a[1])/denominator+a[0])
        included = inside & (vertical > finish_height+.03) & (areas > 1e-10)
        if included.sum() < 10:
            self.statusBar().showMessage("축을 제안할 지대치 표면이 부족합니다.", 6000)
            return
        basis = np.array([1., 0, 0]) if abs(axis[0]) < .8 else np.array([0., 1, 0])
        basis -= axis*np.dot(basis, axis)
        basis /= np.linalg.norm(basis)
        second = np.cross(axis, basis)
        best_axis = axis.copy()
        best_score = float("inf")
        for tilt in [0, 5, 10, 15, 20, 25, 30]:
            for angle in np.linspace(0, 2*np.pi, 24, endpoint=False):
                candidate = axis*math.cos(math.radians(tilt))+(basis*math.cos(angle)+second*math.sin(angle))*math.sin(math.radians(tilt))
                score = float(np.sum(areas[included]*np.maximum(0, -(normals[included] @ candidate))))
                score += areas[included].sum()*.001*(1-np.dot(axis, candidate))
                if score < best_score:
                    best_score, best_axis = score, candidate
        self.push_undo()
        state["axis"] = best_axis.tolist()
        self.axis_info.setText(f"X {best_axis[0]:.3f}   Y {best_axis[1]:.3f}   Z {best_axis[2]:.3f}")
        self.dirty = True
        self.statusBar().showMessage("마진 내부 표면의 법선 역경사를 줄이는 축을 제안했습니다. 삽입 가능성을 직접 확인하세요.", 12000)

    def refine_margin_curvature(self):
        state = self.teeth[str(self.current_tooth)]
        source = self.objects.get(state.get("scan_id"))
        if source is None or len(state.get("margin", [])) < 6:
            self.statusBar().showMessage("단면 윤곽을 먼저 추출하거나 마진 점을 지정하세요.", 6000)
            return
        curve = vtk.vtkCurvatures()
        curve.SetInputData(source["mesh"])
        curve.SetCurvatureTypeToMean()
        curve.Update()
        mesh = curve.GetOutput()
        values = np.abs(vtk_to_numpy(mesh.GetPointData().GetScalars()))
        values = np.nan_to_num(values, nan=0, posinf=0, neginf=0)
        scale = max(float(np.percentile(values, 95)), 1e-6)
        locator = vtk.vtkStaticPointLocator()
        locator.SetDataSet(mesh)
        locator.BuildLocator()
        points = vtk_to_numpy(mesh.GetPoints().GetData())
        result = []
        ids = vtk.vtkIdList()
        for point in state["margin"]:
            locator.FindPointsWithinRadius(.45, point, ids)
            if ids.GetNumberOfIds():
                indices = np.asarray([ids.GetId(j) for j in range(ids.GetNumberOfIds())])
                distance = np.linalg.norm(points[indices]-point, axis=1)
                scores = np.minimum(values[indices]/scale, 2)*np.exp(-(distance/.25)**2)
                result.append(points[indices[int(np.argmax(scores))]].tolist())
            else:
                result.append(point)
        self.push_undo()
        state["margin"] = result
        state["closed"] = False
        self.dirty = True
        self.update_margin()
        self.statusBar().showMessage("0.45 mm 주변의 곡률로 마진 초안을 보정했습니다. 경계를 확인하고 닫으세요.", 10000)

    def create_library_tooth(self, checked=False, tooth_override=None, select_result=True):
        if select_result:
            self.push_undo()
        tooth = int(tooth_override or self.current_tooth)
        height, width, cervical_width, depth, cervical_depth, mesial_cej, distal_cej = self.tooth_dimensions[str(tooth)]
        posterior = tooth % 10 >= 4
        molar = tooth % 10 >= 6
        canine = tooth % 10 == 3
        mirrored = tooth//10 in [2, 4]
        count, rings = 96, 40
        points = [[0., 0., 0.]]
        for level in range(rings):
            t = level/rings
            if t <= .4:
                radial_blend = math.sin(np.pi/2*t/.4)
                rx = (cervical_width+(width-cervical_width)*radial_blend)/2
                ry = (cervical_depth+(depth-cervical_depth)*radial_blend)/2
                radial_factor = 1.
                z = height*.46*t/.4
            else:
                q = (t-.4)/.6
                rx, ry = width/2, depth/2
                radial_factor = math.sqrt(max(0, 1-q*q))
                z = height*(.46+.44*q)
            for angle in np.linspace(0, 2*np.pi, count, endpoint=False):
                c, s = math.cos(angle), math.sin(angle)
                exponent = .72 if posterior else .92
                x = rx*math.copysign(abs(c)**exponent, c)*radial_factor
                y = ry*math.copysign(abs(s)**exponent, s)*radial_factor
                x *= 1-.10*max(0, -s)
                y *= 1-.07*max(0, -c)
                u, v = x/(width/2), y/(depth/2)
                extra = 0.
                if t > .4:
                    strength = min(1, (t-.4)/.25)
                    if molar:
                        extra = .11*height*sum(math.exp(-((u-a)**2+(v-b)**2)/.095) for a, b in [(-.37, -.37), (.37, -.37), (-.37, .37), (.37, .37)])
                        extra -= .035*height*math.exp(-(u*u+v*v)/.06)
                    elif posterior:
                        extra = .14*height*sum(math.exp(-(u*u+(v-b)**2)/.12) for b in [-.38, .38])
                    elif canine:
                        extra = .14*height*math.exp(-(u*u+v*v)/.18)
                    else:
                        extra = .10*height*math.exp(-(v/.32)**2)*(1-.25*u*u)
                    extra *= strength
                cej = (mesial_cej*max(c, 0)**3+distal_cej*max(-c, 0)**3)*max(0, 1-t/.35)
                points.append([-x if mirrored else x, y, z+extra+cej])
        points.append([0., 0., height*.9])
        points = np.asarray(points, dtype=float)
        points[:, 2] *= height/points[:, 2].max()
        faces = []
        for j in range(count):
            k = (j+1)%count
            faces.append([0, 1+k, 1+j])
            for ring in range(rings-1):
                a, b = 1+ring*count+j, 1+ring*count+k
                faces.extend([[a, b, b+count], [a, b+count, a+count]])
            faces.append([len(points)-1, 1+(rings-1)*count+j, 1+(rings-1)*count+k])
        faces = np.asarray(faces, dtype=np.int64)
        if mirrored:
            faces = faces[:, [0, 2, 1]]
        vtk_points = vtk.vtkPoints()
        vtk_points.SetData(numpy_to_vtk(points, deep=True))
        cells = vtk.vtkCellArray()
        cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).ravel(), deep=True))
        mesh = vtk.vtkPolyData()
        mesh.SetPoints(vtk_points)
        mesh.SetPolys(cells)
        key = self.add_object(mesh, f"FDI {tooth} · 기본 치아형", "library", tooth=tooth, visible=False,
                              library_origin="DentCraft 치수 기반 자체 매개변수 형태", source_license="MIT")
        if select_result:
            self.library_combo.setCurrentIndex(self.library_combo.findData(key))
            self.statusBar().showMessage(f"{tooth}번 기본 치아 형태를 등록했습니다. 외부 STL 형태로 교체할 수 있습니다.", 8000)
        return key

    def generate_pontic(self):
        state = self.teeth[str(self.current_tooth)]
        position = state.get("placement")
        if position is None:
            self.statusBar().showMessage("폰틱 위치 선택을 누르고 경부를 놓을 위치를 클릭하세요.", 7000)
            return
        source_id = self.library_combo.currentData()
        self.push_undo()
        if source_id not in self.objects:
            source_id = self.create_library_tooth(select_result=False)
        source = self.objects[source_id]["mesh"]
        points = vtk_to_numpy(source.GetPoints().GetData()).astype(float).copy()
        bounds = np.asarray(source.GetBounds()).reshape(3, 2)
        points -= np.array([bounds[0].mean(), bounds[1].mean(), bounds[2, 0]])
        points *= self.library_scale.value()
        angle = math.radians(self.library_rotation.value())
        rotation = np.array([[math.cos(angle), -math.sin(angle), 0], [math.sin(angle), math.cos(angle), 0], [0, 0, 1]])
        axis = np.asarray(state["axis"], dtype=float)
        axis /= np.linalg.norm(axis)
        x = np.array([1., 0, 0]) if abs(axis[0]) < .85 else np.array([0., 1, 0])
        x -= axis*np.dot(x, axis)
        x /= np.linalg.norm(x)
        basis = np.column_stack([x, np.cross(axis, x), axis])
        points = points @ rotation.T @ basis.T+position
        mesh = vtk.vtkPolyData()
        mesh.DeepCopy(source)
        vtk_points = vtk.vtkPoints()
        vtk_points.SetData(numpy_to_vtk(points, deep=True))
        mesh.SetPoints(vtk_points)
        self.add_object(mesh, f"{self.current_tooth} · 폰틱", "pontic", tooth=self.current_tooth, axis=axis.tolist(), library_id=source_id)
        self.focus_selection()
        self.statusBar().showMessage("폰틱을 생성했습니다. 기저면·교합·연결부를 설계하고 검사하세요.", 9000)

    def update_layout_path(self):
        if self.path_actor:
            self.renderer.RemoveActor(self.path_actor)
            self.path_actor = None
        if self.layout_points:
            points = vtk.vtkPoints()
            for point in self.layout_points:
                points.InsertNextPoint(point)
            data = vtk.vtkPolyData()
            data.SetPoints(points)
            lines = vtk.vtkCellArray()
            lines.InsertNextCell(len(self.layout_points))
            for index in range(len(self.layout_points)):
                lines.InsertCellPoint(index)
            data.SetLines(lines)
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(data)
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(.33, .94, .72)
            actor.GetProperty().SetLineWidth(3)
            actor.GetProperty().LightingOff()
            actor.PickableOff()
            self.renderer.AddActor(actor)
            self.path_actor = actor
        self.vtk_widget.GetRenderWindow().Render()

    def clear_layout_path(self):
        self.push_undo()
        self.layout_points = []
        self.update_layout_path()
        self.dirty = True

    def arrange_teeth(self):
        if len(self.layout_points) < 2:
            self.statusBar().showMessage("치아 배열의 경부 경로를 최소 2점 지정하세요.", 7000)
            return
        path = np.asarray(self.layout_points, dtype=float)
        lengths = np.r_[0, np.cumsum(np.linalg.norm(np.diff(path, axis=0), axis=1))]
        if lengths[-1] < 3:
            self.statusBar().showMessage("배치 경로가 너무 짧습니다.", 6000)
            return
        count = self.setup_count.value()
        arch = [18, 17, 16, 15, 14, 13, 12, 11, 21, 22, 23, 24, 25, 26, 27, 28] if self.current_tooth//10 < 3 else [48, 47, 46, 45, 44, 43, 42, 41, 31, 32, 33, 34, 35, 36, 37, 38]
        numbers = arch[(16-count)//2:(16-count)//2+count]
        axis = np.asarray(self.teeth[str(self.current_tooth)]["axis"], dtype=float)
        axis /= np.linalg.norm(axis)
        library_id = self.library_combo.currentData()
        self.push_undo()
        for position, tooth in zip(np.linspace(0, lengths[-1], count), numbers):
            segment = min(len(path)-2, max(0, int(np.searchsorted(lengths, position, side="right")-1)))
            fraction = (position-lengths[segment])/max(lengths[segment+1]-lengths[segment], 1e-9)
            center = path[segment]*(1-fraction)+path[segment+1]*fraction
            tangent = path[segment+1]-path[segment]
            tangent -= axis*np.dot(tangent, axis)
            if np.linalg.norm(tangent) < 1e-6:
                continue
            tangent /= np.linalg.norm(tangent)
            source_id = library_id if library_id in self.objects else self.create_library_tooth(tooth_override=tooth, select_result=False)
            source = self.objects[source_id]["mesh"]
            points = vtk_to_numpy(source.GetPoints().GetData()).astype(float).copy()
            bounds = np.asarray(source.GetBounds()).reshape(3, 2)
            points -= [bounds[0].mean(), bounds[1].mean(), bounds[2, 0]]
            points *= self.library_scale.value()
            basis = np.column_stack([tangent, np.cross(axis, tangent), axis])
            points = points @ basis.T+center
            mesh = vtk.vtkPolyData()
            mesh.DeepCopy(source)
            vtk_points = vtk.vtkPoints()
            vtk_points.SetData(numpy_to_vtk(points, deep=True))
            mesh.SetPoints(vtk_points)
            self.add_object(mesh, f"{tooth} · 배열 치아", "pontic", tooth=tooth, axis=axis.tolist(), library_id=source_id, setup=True)
        self.camera_view("fit")
        self.statusBar().showMessage("경로에 치아를 배열했습니다. 인접면·기저면·교합을 조정하세요.", 10000)

    def build_path_bar(self):
        if len(self.layout_points) < 2:
            self.statusBar().showMessage("바 경로를 최소 2점 선택하세요.", 6000)
            return
        points = vtk.vtkPoints()
        for point in self.layout_points:
            points.InsertNextPoint(point)
        spline = vtk.vtkParametricSpline()
        spline.SetPoints(points)
        sample = vtk.vtkParametricFunctionSource()
        sample.SetParametricFunction(spline)
        sample.SetUResolution(max(40, len(self.layout_points)*20))
        tube = vtk.vtkTubeFilter()
        tube.SetInputConnection(sample.GetOutputPort())
        tube.SetRadius(self.connector_radius.value())
        tube.SetNumberOfSides(48)
        tube.CappingOn()
        tri = vtk.vtkTriangleFilter()
        tri.SetInputConnection(tube.GetOutputPort())
        clean = vtk.vtkCleanPolyData()
        clean.SetInputConnection(tri.GetOutputPort())
        clean.Update()
        try:
            mesh, report, solid = self.analyze_print_mesh(clean.GetOutput(), "연결 바")
            self.push_undo()
            self.add_object(mesh, "경로 연결 바", "bar", path=copy.deepcopy(self.layout_points), radius=self.connector_radius.value())
            self.statusBar().showMessage(f"연결 바 생성 · 단면적 {np.pi*self.connector_radius.value()**2:.2f} mm² · 유지부는 별도 설계하세요.", 10000)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "바 생성 실패", str(error))

    def import_dicom(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "DICOM 시리즈 폴더")
        if not folder:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            import SimpleITK as sitk
            series = list(sitk.ImageSeriesReader.GetGDCMSeriesIDs(folder))
            if not series:
                raise ValueError("폴더에서 DICOM 시리즈를 찾지 못했습니다.")
            groups = [list(sitk.ImageSeriesReader.GetGDCMSeriesFileNames(folder, uid)) for uid in series]
            index = int(np.argmax([len(files) for files in groups]))
            if len(groups) > 1:
                QtWidgets.QApplication.restoreOverrideCursor()
                choice, accepted = QtWidgets.QInputDialog.getItem(self, "DICOM 시리즈 선택", "불러올 시리즈", [f"{i+1} · {len(files)}장" for i, files in enumerate(groups)], index, False)
                QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
                if not accepted:
                    return
                index = int(choice.split(" · ")[0])-1
            reader = sitk.ImageSeriesReader()
            reader.SetFileNames(groups[index])
            volume = reader.Execute()
            if volume.GetDimension() != 3 or min(volume.GetSize()) < 2:
                raise ValueError("3차원 시리즈가 아닙니다.")
            original_size = volume.GetSize()
            factor = max(1, int(math.ceil((np.prod(original_size)/18_000_000)**(1/3))))
            if factor > 1:
                volume = sitk.BinShrink(volume, [factor]*3)
            self.ct_volume = sitk.GetArrayFromImage(volume).astype(np.float32)
            self.ct_metadata = {"spacing": list(volume.GetSpacing()), "origin": list(volume.GetOrigin()),
                                "direction": list(volume.GetDirection()), "factor": factor, "size": list(volume.GetSize())}
            self.ct_slice.setRange(0, len(self.ct_volume)-1)
            self.ct_slice.setValue(len(self.ct_volume)//2)
            self.update_ct_preview()
            spacing = self.ct_metadata["spacing"]
            self.ct_info.setText(f"{volume.GetSize()} 복셀\n간격 {spacing[0]:.3f} × {spacing[1]:.3f} × {spacing[2]:.3f} mm\n원본 대비 축소 {factor}배 · LPS 좌표")
            self.statusBar().showMessage("DICOM 시리즈를 읽었습니다. 임계 강도를 지정해 표면을 생성하세요.", 10000)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "DICOM 불러오기 실패", str(error))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def update_print_bed(self, *args):
        if self.bed_actor:
            self.renderer.RemoveActor(self.bed_actor)
            self.bed_actor = None
        if not any(obj["role"] == "print" for obj in self.objects.values()):
            return
        width, depth = self.printer_bed_x.value(), self.printer_bed_y.value()
        plane = vtk.vtkPlaneSource()
        plane.SetOrigin(0, 0, -.08)
        plane.SetPoint1(width, 0, -.08)
        plane.SetPoint2(0, depth, -.08)
        plane.SetXResolution(max(1, int(width/10)))
        plane.SetYResolution(max(1, int(depth/10)))
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(plane.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetRepresentationToWireframe()
        actor.GetProperty().SetColor(.23, .42, .49)
        actor.GetProperty().SetOpacity(.55)
        actor.GetProperty().LightingOff()
        actor.PickableOff()
        self.renderer.AddActor(actor)
        self.bed_actor = actor
        self.vtk_widget.GetRenderWindow().Render()

    def update_ct_preview(self, *args):
        if self.ct_volume is None:
            return
        values = self.ct_volume[self.ct_slice.value()]
        low, high = np.percentile(self.ct_volume[::max(1, len(self.ct_volume)//16)], [1, 99])
        pixels = np.ascontiguousarray(np.clip((values-low)/max(high-low, 1)*255, 0, 255).astype(np.uint8))
        image = QtGui.QImage(pixels.data, pixels.shape[1], pixels.shape[0], pixels.strides[0], QtGui.QImage.Format.Format_Grayscale8).copy()
        self.ct_preview.setPixmap(QtGui.QPixmap.fromImage(image).scaled(280, 240, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation))

    def build_dicom_surface(self):
        if self.ct_volume is None:
            self.statusBar().showMessage("먼저 DICOM 시리즈를 불러오세요.", 5000)
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            values = self.ct_volume
            threshold = self.ct_threshold.value()
            if not values.min() < threshold < values.max():
                raise ValueError("임계 강도가 영상 범위 밖입니다.")
            image = vtk.vtkImageData()
            image.SetDimensions(values.shape[2], values.shape[1], values.shape[0])
            image.SetSpacing(self.ct_metadata["spacing"])
            image.GetPointData().SetScalars(numpy_to_vtk(values.ravel(), deep=True))
            contour = vtk.vtkFlyingEdges3D()
            contour.SetInputData(image)
            contour.SetValue(0, threshold)
            contour.Update()
            surface = contour.GetOutput()
            if surface.GetNumberOfPolys() == 0:
                raise ValueError("임계 강도에서 표면을 얻지 못했습니다.")
            if self.ct_largest.isChecked():
                connectivity = vtk.vtkPolyDataConnectivityFilter()
                connectivity.SetInputData(surface)
                connectivity.SetExtractionModeToLargestRegion()
                connectivity.Update()
                surface = connectivity.GetOutput()
            if surface.GetNumberOfPolys() > 800000:
                reduce = vtk.vtkDecimatePro()
                reduce.SetInputData(surface)
                reduce.SetTargetReduction(1-800000/surface.GetNumberOfPolys())
                reduce.PreserveTopologyOn()
                reduce.Update()
                surface = reduce.GetOutput()
            mesh = vtk.vtkPolyData()
            mesh.DeepCopy(surface)
            coordinates = vtk_to_numpy(mesh.GetPoints().GetData()).astype(float)
            direction = np.asarray(self.ct_metadata["direction"]).reshape(3, 3)
            coordinates = coordinates @ direction.T+np.asarray(self.ct_metadata["origin"])
            points = vtk.vtkPoints()
            points.SetData(numpy_to_vtk(coordinates, deep=True))
            mesh.SetPoints(points)
            if np.linalg.det(direction) < 0:
                reverse = vtk.vtkReverseSense()
                reverse.SetInputData(mesh)
                reverse.ReverseCellsOn()
                reverse.Update()
                mesh.DeepCopy(reverse.GetOutput())
            self.push_undo()
            self.add_object(mesh, f"CT 분할 · {threshold:g}", "segmentation", threshold=threshold, dicom_geometry=copy.deepcopy(self.ct_metadata))
            self.camera_view("fit")
            self.statusBar().showMessage("DICOM 표면을 생성했습니다. 영상 경계의 열린 표면은 모델 생성 후 출력하세요.", 10000)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "DICOM 표면 생성 실패", str(error))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def generate_crown(self):
        if self.busy:
            return
        state = self.teeth[str(self.current_tooth)]
        if not state.get("closed") or len(state["margin"]) < 6:
            QtWidgets.QMessageBox.information(self, "마진선 필요", "마진 점을 최소 6개 지정하고 마진을 닫아주세요.")
            return
        scan_id = state.get("scan_id")
        if scan_id not in self.objects:
            scan_id = next((k for k, o in self.objects.items() if o["role"] == "scan"), None)
        if scan_id is None:
            QtWidgets.QMessageBox.information(self, "작업악 필요", "먼저 작업악 스캔을 불러와 주세요.")
            return
        self.busy = True
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        self.statusBar().showMessage("지대치 내면과 크라운 표면을 계산하고 있습니다…")
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        try:
            axis = np.asarray(state["axis"], dtype=float)
            axis /= np.linalg.norm(axis)
            basis_x = np.array([1., 0, 0]) if abs(axis[0]) < 0.85 else np.array([0., 1, 0])
            basis_x -= axis*np.dot(basis_x, axis)
            basis_x /= np.linalg.norm(basis_x)
            basis_y = np.cross(axis, basis_x)
            basis = np.column_stack([basis_x, basis_y, axis])
            margin = np.asarray(state["margin"], dtype=float)
            origin = margin.mean(axis=0)
            local = (margin-origin) @ basis
            angles = np.arctan2(local[:, 1], local[:, 0])
            angular_steps = np.diff(np.unwrap(np.r_[angles, angles[0]]))
            if not (np.all(angular_steps > -0.001) or np.all(angular_steps < 0.001)):
                raise ValueError("현재 버전은 삽입축에서 방사형으로 보이는 마진을 지원합니다. 점의 순서와 삽입축을 확인하세요.")
            order = np.argsort(angles)
            angles, local = angles[order], local[order]
            unique = np.r_[True, np.diff(angles) > 1e-4]
            angles, local = angles[unique], local[unique]
            if len(angles) < 6:
                raise ValueError("삽입축에서 마진의 윤곽을 구분할 수 없습니다.")
            radii = np.linalg.norm(local[:, :2], axis=1)
            if radii.min() < 0.15 or radii.max()/radii.min() > 5:
                raise ValueError("마진이 지나치게 찌그러져 있습니다. 삽입축과 마진을 확인하세요.")
            sampled_angles = np.linspace(angles[0], angles[0]+2*np.pi, 96, endpoint=False)
            cyclic_angles = np.r_[angles, angles[0]+2*np.pi]
            boundary_radii = np.interp(sampled_angles, cyclic_angles, np.r_[radii, radii[0]])
            boundary_z = np.interp(sampled_angles, cyclic_angles, np.r_[local[:, 2], local[0, 2]])
            radial = np.column_stack([np.cos(sampled_angles), np.sin(sampled_angles)])
            params = {key: control.value() for key, control in self.design_controls.items()}
            params.update(type=self.restoration_type.currentIndex(), shape=self.shape_type.currentIndex(), material=self.material.currentIndex())
            params.update(library_id=self.library_combo.currentData(), library_scale=self.library_scale.value(), library_rotation=self.library_rotation.value())
            scan = self.objects[scan_id]["mesh"]
            tree = vtk.vtkOBBTree()
            tree.SetDataSet(scan)
            tree.BuildLocator()
            scan_points = vtk_to_numpy(scan.GetPoints().GetData())
            z_values = (scan_points-origin) @ axis
            z_min, z_max = z_values.min()-5, z_values.max()+5
            count, rings = 96, 26
            inner = []
            missing = 0
            hits = vtk.vtkPoints()
            ids = vtk.vtkIdList()
            for ring in range(rings+1):
                fraction = ring/rings
                samples = [0] if ring == 0 else range(count)
                for j in samples:
                    xy = radial[j]*boundary_radii[j]*fraction
                    start = origin+basis_x*xy[0]+basis_y*xy[1]+axis*z_max
                    end = origin+basis_x*xy[0]+basis_y*xy[1]+axis*z_min
                    hits.Reset()
                    ids.Reset()
                    tree.IntersectWithLine(start, end, hits, ids)
                    if ring == rings:
                        point = origin+basis_x*(xy[0]+radial[j, 0]*params["gap"])+basis_y*(xy[1]+radial[j, 1]*params["gap"])+axis*boundary_z[j]
                        if params["type"] == 2:
                            inset = min(params["edge"]+params["gap"], boundary_radii[j]*.025)
                            point = origin+basis_x*(xy[0]-radial[j, 0]*inset)+basis_y*(xy[1]-radial[j, 1]*inset)+axis*(boundary_z[j]-inset)
                    elif hits.GetNumberOfPoints():
                        candidates = np.array([hits.GetPoint(k) for k in range(hits.GetNumberOfPoints())])
                        heights = (candidates-origin) @ axis
                        chosen = int(np.argmax(heights))
                        point = candidates[chosen]
                        cell = scan.GetCell(ids.GetId(chosen))
                        vertices = [np.array(scan.GetPoint(cell.GetPointId(k))) for k in range(min(3, cell.GetNumberOfPoints()))]
                        normal = np.cross(vertices[1]-vertices[0], vertices[2]-vertices[0])
                        length = np.linalg.norm(normal)
                        normal = normal/length if length > 1e-10 else axis.copy()
                        if np.dot(normal, axis) < 0:
                            normal *= -1
                        point = point+params["gap"]*normal
                    else:
                        missing += 1
                        point = origin+basis_x*xy[0]+basis_y*xy[1]+axis*(boundary_z[j]+params["gap"])
                    inner.append(point)
            if missing:
                raise ValueError(f"마진 내부 {missing}개 위치에서 지대치 표면을 찾지 못했습니다. 마진과 삽입축을 확인하세요.")
            inner = np.asarray(inner)
            inner_local = (inner-origin) @ basis
            prep_top = np.percentile(inner_local[:, 2], 90)
            library_tree = None
            if params["library_id"] in self.objects and params["type"] not in [1, 4]:
                source = self.objects[params["library_id"]]["mesh"]
                library_mesh = vtk.vtkPolyData()
                library_mesh.DeepCopy(source)
                library_points = vtk_to_numpy(source.GetPoints().GetData()).astype(float).copy()
                library_bounds = np.asarray(source.GetBounds()).reshape(3, 2)
                library_points -= [library_bounds[0].mean(), library_bounds[1].mean(), library_bounds[2, 0]]
                angle = math.radians(params["library_rotation"])
                rotation = np.array([[math.cos(angle), -math.sin(angle), 0], [math.sin(angle), math.cos(angle), 0], [0, 0, 1]])
                library_points = library_points @ rotation.T*params["library_scale"]
                vtk_points = vtk.vtkPoints()
                vtk_points.SetData(numpy_to_vtk(library_points, deep=True))
                library_mesh.SetPoints(vtk_points)
                library_tree = vtk.vtkOBBTree()
                library_tree.SetDataSet(library_mesh)
                library_tree.BuildLocator()
                library_low, library_high = library_points[:, 2].min()-1, library_points[:, 2].max()+1
                library_hits = vtk.vtkPoints()
            outer = []
            for ring in range(rings+1):
                fraction = ring/rings
                samples = [0] if ring == 0 else range(count)
                for j in samples:
                    idx = 0 if ring == 0 else 1+(ring-1)*count+j
                    p = inner_local[idx].copy()
                    boundary_radius = boundary_radii[j]
                    direction = radial[j]
                    if params["type"] in [1, 4]:
                        radius = np.linalg.norm(p[:2])
                        p[:2] += direction*params["wall"]*fraction
                        p[2] += params["wall"]*(1-fraction**10)
                    else:
                        envelope = boundary_radius*params["width"]*fraction
                        envelope += params["wall"]*math.sin(np.pi*fraction)*0.6
                        envelope = max(envelope, np.linalg.norm(p[:2])+params["wall"]*fraction*max(0, 1-fraction**12)**0.25)
                        if params["type"] == 2:
                            envelope = np.linalg.norm(p[:2])
                        p[:2] = direction*envelope
                        u, v = fraction*direction
                        if params["shape"] == 0:
                            relief = sum(np.exp(-((u-cx)**2+(v-cy)**2)/0.11) for cx, cy in [(-.4, -.4), (.4, -.4), (-.4, .4), (.4, .4)])
                        elif params["shape"] == 1:
                            relief = sum(np.exp(-((u-cx)**2+(v-cy)**2)/0.15) for cx, cy in [(0, -.42), (0, .42)])
                        else:
                            relief = np.exp(-(v/.35)**2)*(1-u*u)
                        top = prep_top+params["wall"]+params["height"]+params["cusp"]*relief
                        if params["type"] == 2:
                            top = boundary_z[j]+params["height"]+params["cusp"]*relief*.2
                        if library_tree is not None and fraction < .94:
                            library_hits.Reset()
                            library_tree.IntersectWithLine([p[0], p[1], library_high], [p[0], p[1], library_low], library_hits, None)
                            if not library_hits.GetNumberOfPoints():
                                raise ValueError("치아 라이브러리가 마진의 편집 영역을 덮지 못합니다. 배율·회전을 조정하거나 다른 치아형을 사용하세요.")
                            top = max(library_hits.GetPoint(k)[2] for k in range(library_hits.GetNumberOfPoints()))
                            if params["type"] == 2:
                                top = min(top, boundary_z[j]+params["height"]+params["cusp"]*relief)
                        shoulder = math.sqrt(max(0, 1-fraction**6))
                        p[2] = boundary_z[j]+(top-boundary_z[j])*shoulder
                        p[2] = max(p[2], inner_local[idx, 2]+params["wall"]*(1-fraction**12))
                    if ring == rings:
                        p[:2] = direction*(boundary_radius+params["gap"]+params["edge"])
                        if params["type"] == 2:
                            p[:2] = direction*boundary_radius
                        p[2] = boundary_z[j]
                    outer.append(origin+basis @ p)
            vertices = np.vstack([inner, outer])
            offset = len(inner)
            faces = []
            for side in [0, 1]:
                base = side*offset
                side_faces = []
                for j in range(count):
                    side_faces.append([base, base+1+j, base+1+(j+1)%count])
                for ring in range(1, rings):
                    previous, following = base+1+(ring-1)*count, base+1+ring*count
                    for j in range(count):
                        k = (j+1)%count
                        side_faces.extend([[previous+j, following+j, following+k], [previous+j, following+k, previous+k]])
                if side == 0:
                    side_faces = [[a, c, b] for a, b, c in side_faces]
                faces.extend(side_faces)
            start = 1+(rings-1)*count
            for j in range(count):
                k = (j+1)%count
                faces.extend([[start+j, offset+start+k, offset+start+j], [start+j, start+k, offset+start+k]])
            inner_surface = vtk.vtkPolyData()
            inner_points = vtk.vtkPoints()
            inner_points.SetData(numpy_to_vtk(inner, deep=True))
            inner_surface.SetPoints(inner_points)
            inner_faces = np.asarray([face for face in faces if max(face) < offset])
            inner_cells = vtk.vtkCellArray()
            inner_cells.SetCells(len(inner_faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(inner_faces), 3), inner_faces]).astype(np.int64).ravel(), deep=True))
            inner_surface.SetPolys(inner_cells)
            inner_distance = vtk.vtkImplicitPolyDataDistance()
            inner_distance.SetInput(inner_surface)
            for index in range(offset, offset+1+(rings-2)*count):
                for attempt in range(8):
                    distance = inner_distance.EvaluateFunction(vertices[index])
                    if distance <= -params["wall"]+1e-5:
                        break
                    gradient = [0., 0., 0.]
                    inner_distance.EvaluateGradient(vertices[index], gradient)
                    gradient = np.asarray(gradient)
                    length = np.linalg.norm(gradient)
                    if length < 1e-8:
                        raise ValueError("외면 두께 보정에 실패했습니다. 마진과 설계 값을 확인하세요.")
                    vertices[index] -= gradient/length*(distance+params["wall"]+.002)
                else:
                    if inner_distance.EvaluateFunction(vertices[index]) > -params["wall"]+1e-5:
                        raise ValueError("외면 두께를 확보하지 못했습니다. 마진과 설계 값을 확인하세요.")
            points = vtk.vtkPoints()
            points.SetData(numpy_to_vtk(vertices, deep=True))
            cells = vtk.vtkCellArray()
            packed = np.column_stack([np.full(len(faces), 3), np.asarray(faces)]).astype(np.int64).ravel()
            cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(packed, deep=True))
            mesh = vtk.vtkPolyData()
            mesh.SetPoints(points)
            mesh.SetPolys(cells)
            triangle_points = vertices[np.asarray(faces)]
            areas = np.linalg.norm(np.cross(triangle_points[:, 1]-triangle_points[:, 0], triangle_points[:, 2]-triangle_points[:, 0]), axis=1)
            if not np.isfinite(vertices).all() or areas.min() < 1e-10:
                raise ValueError("퇴화된 삼각형이 발생했습니다. 마진 간격과 크라운 값을 확인하세요.")
            edges = vtk.vtkFeatureEdges()
            edges.SetInputData(mesh)
            edges.BoundaryEdgesOn()
            edges.NonManifoldEdgesOn()
            edges.FeatureEdgesOff()
            edges.ManifoldEdgesOff()
            edges.Update()
            if edges.GetOutput().GetNumberOfCells():
                raise ValueError("크라운 메시를 닫지 못했습니다. 마진을 다시 지정하세요.")
            self.push_undo()
            old = [key for key, obj in self.objects.items() if obj.get("tooth") == self.current_tooth and obj["role"] == "restoration"]
            for key in old:
                self.renderer.RemoveActor(self.objects[key]["actor"])
                del self.objects[key]
            for key in list(self.objects):
                if any(item in self.objects[key].get("endpoints", []) for item in old):
                    self.renderer.RemoveActor(self.objects[key]["actor"])
                    del self.objects[key]
            editable = np.zeros(len(vertices), dtype=bool)
            editable[offset:offset+1+(rings-2)*count] = True
            restoration_name = ["크라운", "코핑", "인레이 기본형", "온레이 기본형", "베니어 기본형"][params["type"]]
            key = self.add_object(mesh, f"{self.current_tooth} · {restoration_name}", "restoration",
                                  tooth=self.current_tooth, inner_count=offset, editable=editable.tolist(), params=params,
                                  axis=axis.tolist(), scan_id=scan_id, ring_points=count)
            state["scan_id"] = scan_id
            state["generated_margin"] = copy.deepcopy(state["margin"])
            self.set_step(4)
            self.focus_selection()
            self.statusBar().showMessage(f"{self.current_tooth}번 보철 생성 완료 · {len(faces):,} 삼각형 · 검사 단계에서 두께를 확인하세요.", 15000)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "크라운 생성 실패", str(error))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.busy = False

    def apply_brush(self, position):
        if self.active_id not in self.objects:
            return
        obj = self.objects[self.active_id]
        mesh = obj["mesh"]
        points = vtk_to_numpy(mesh.GetPoints().GetData())
        editable = np.asarray(obj.get("editable", [obj["role"] == "pontic"]*len(points)), dtype=bool)
        distance = np.linalg.norm(points-position, axis=1)
        mask = (distance < self.brush_radius.value()) & editable
        if not mask.any():
            return
        weight = np.exp(-4*(distance[mask]/self.brush_radius.value())**2)
        triangles = vtk_to_numpy(mesh.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
        inner_distance = None
        if obj.get("inner_count", 0):
            inner_triangles = triangles[np.all(triangles < obj["inner_count"], axis=1)]
            inner_surface = vtk.vtkPolyData()
            inner_surface.SetPoints(mesh.GetPoints())
            inner_cells = vtk.vtkCellArray()
            inner_cells.SetCells(len(inner_triangles), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(inner_triangles), 3), inner_triangles]).astype(np.int64).ravel(), deep=True))
            inner_surface.SetPolys(inner_cells)
            inner_distance = vtk.vtkImplicitPolyDataDistance()
            inner_distance.SetInput(inner_surface)
        if self.mode == "smooth":
            total = np.zeros_like(points)
            counts = np.zeros(len(points))
            for a, b in [(0, 1), (1, 2), (2, 0), (1, 0), (2, 1), (0, 2)]:
                np.add.at(total, triangles[:, a], points[triangles[:, b]])
                np.add.at(counts, triangles[:, a], 1)
            target = total[mask]/np.maximum(counts[mask, None], 1)
            proposed = points[mask]+(target-points[mask])*(weight[:, None]*min(0.6, self.brush_strength.value()*4))
        else:
            normals_filter = vtk.vtkPolyDataNormals()
            normals_filter.SetInputData(mesh)
            normals_filter.SplittingOff()
            normals_filter.ConsistencyOn()
            normals_filter.Update()
            normals = vtk_to_numpy(normals_filter.GetOutput().GetPointData().GetNormals())
            delta = normals[mask]*weight[:, None]*self.brush_strength.value()*(1 if self.mode == "add" else -1)
            proposed = points[mask]+delta
        if inner_distance is not None:
            signed_distance = np.asarray([inner_distance.EvaluateFunction(p) for p in proposed])
            safe = signed_distance <= -max(.20, obj.get("params", {}).get("wall", .20))
            proposed[~safe] = points[mask][~safe]
        points[mask] = proposed
        mesh.GetPoints().Modified()
        mesh.Modified()
        updated_normals = vtk.vtkPolyDataNormals()
        updated_normals.SetInputData(mesh)
        updated_normals.SplittingOff()
        updated_normals.ConsistencyOn()
        updated_normals.Update()
        mesh.GetPointData().SetNormals(updated_normals.GetOutput().GetPointData().GetNormals())
        mesh.GetPointData().SetScalars(None)
        obj["actor"].GetMapper().ScalarVisibilityOff()
        self.last_brush_time = time.monotonic()
        self.dirty = True
        self.vtk_widget.GetRenderWindow().Render()

    def push_undo(self):
        snapshot = {"objects": {}, "teeth": copy.deepcopy(self.teeth), "current": self.current_tooth, "layout_points": copy.deepcopy(self.layout_points)}
        for key, obj in self.objects.items():
            mesh = vtk.vtkPolyData()
            mesh.DeepCopy(obj["mesh"])
            metadata = {k: copy.deepcopy(v) for k, v in obj.items() if k not in ["mesh", "actor"]}
            metadata.update(visible=bool(obj["actor"].GetVisibility()), opacity=obj["actor"].GetProperty().GetOpacity())
            snapshot["objects"][key] = {"mesh": mesh, "metadata": metadata}
        self.undo_stack.append(snapshot)
        while len(self.undo_stack) > 12 or (len(self.undo_stack) > 1 and sum(sum(o["mesh"].GetActualMemorySize() for o in s["objects"].values()) for s in self.undo_stack) > 300000):
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self):
        if not self.undo_stack:
            self.statusBar().showMessage("실행 취소할 작업이 없습니다.", 3000)
            return
        target = self.undo_stack.pop()
        previous_redo = self.redo_stack.copy()
        self.push_undo()
        current = self.undo_stack.pop()
        self.redo_stack = previous_redo+[current]
        self.restore_snapshot(target)

    def redo(self):
        if not self.redo_stack:
            return
        target = self.redo_stack.pop()
        remaining = self.redo_stack.copy()
        self.push_undo()
        self.redo_stack = remaining
        self.restore_snapshot(target)

    def restore_snapshot(self, snapshot):
        self.clear_scene()
        self.teeth = copy.deepcopy(snapshot["teeth"])
        self.layout_points = copy.deepcopy(snapshot.get("layout_points", []))
        self.update_layout_path()
        for key, entry in snapshot["objects"].items():
            metadata = copy.deepcopy(entry["metadata"])
            name, role = metadata.pop("name"), metadata.pop("role")
            self.add_object(entry["mesh"], name, role, key=key, **metadata)
        self.select_tooth(snapshot["current"])
        self.dirty = True

    def camera_view(self, preset):
        camera = self.renderer.GetActiveCamera()
        if preset == "fit":
            self.renderer.ResetCamera()
        else:
            bounds = self.renderer.ComputeVisiblePropBounds()
            center = np.array([(bounds[0]+bounds[1])/2, (bounds[2]+bounds[3])/2, (bounds[4]+bounds[5])/2]) if bounds[0] < bounds[1] else np.zeros(3)
            direction = {"top": [0, 0, 1], "front": [0, -1, 0.12], "left": [-1, 0, .1], "right": [1, 0, .1], "iso": [.4, -1, 1]}[preset]
            camera.SetFocalPoint(center)
            camera.SetPosition(center+np.asarray(direction)*100)
            camera.SetViewUp(0, 1, 0) if preset == "top" else camera.SetViewUp(0, 0, 1)
            self.renderer.ResetCamera()
        self.renderer.ResetCameraClippingRange()
        self.vtk_widget.GetRenderWindow().Render()

    def focus_selection(self):
        points = self.teeth.get(str(self.current_tooth), {}).get("margin", [])
        if self.active_id in self.objects and self.objects[self.active_id]["role"] not in ["scan", "antagonist"]:
            bounds = self.objects[self.active_id]["mesh"].GetBounds()
        elif points:
            values = np.asarray(points)
            lo, hi = values.min(axis=0), values.max(axis=0)
            hi[2] += 7
            bounds = [lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]]
        elif self.active_id in self.objects:
            bounds = self.objects[self.active_id]["mesh"].GetBounds()
        else:
            return
        self.renderer.ResetCamera(bounds)
        self.renderer.ResetCameraClippingRange()
        self.vtk_widget.GetRenderWindow().Render()

    def toggle_wire(self, checked):
        for obj in self.objects.values():
            if checked:
                obj["actor"].GetProperty().SetRepresentationToWireframe()
            else:
                obj["actor"].GetProperty().SetRepresentationToSurface()
        self.vtk_widget.GetRenderWindow().Render()

    def toggle_section(self, checked):
        self.section_enabled = checked
        self.section_changed()
        for obj in self.objects.values():
            obj["actor"].GetMapper().RemoveAllClippingPlanes()
            if checked:
                obj["actor"].GetMapper().AddClippingPlane(self.section_plane)
        self.vtk_widget.GetRenderWindow().Render()

    def section_changed(self, *args):
        if not self.objects:
            return
        obj = self.objects.get(self.active_id) or next(iter(self.objects.values()))
        bounds = obj["mesh"].GetBounds()
        axis = self.section_axis.currentIndex()
        origin = [(bounds[i*2]+bounds[i*2+1])/2 for i in range(3)]
        origin[axis] = bounds[axis*2]+self.section_slider.value()/1000*(bounds[axis*2+1]-bounds[axis*2])
        normal = [0, 0, 0]
        normal[axis] = 1
        self.section_plane.SetNormal(normal)
        self.section_plane.SetOrigin(origin)
        self.vtk_widget.GetRenderWindow().Render()

    def measure_reset(self):
        for actor in self.measure_actors:
            self.renderer.RemoveActor(actor)
        self.measure_actors = []
        self.measure_points = []
        self.vtk_widget.GetRenderWindow().Render()

    def show_distances(self, kind):
        if self.scalar_bar:
            self.renderer.RemoveViewProp(self.scalar_bar)
            self.scalar_bar = None
        if kind is None:
            for obj in self.objects.values():
                obj["actor"].GetMapper().ScalarVisibilityOff()
            self.color_mode = None
            self.vtk_widget.GetRenderWindow().Render()
            return
        obj = self.objects.get(self.active_id)
        if not obj or obj["role"] != "restoration":
            obj = next((o for o in self.objects.values() if o["role"] == "restoration" and o.get("tooth") == self.current_tooth), None)
        if obj is None:
            self.statusBar().showMessage("생성한 보철을 선택하세요.", 5000)
            return
        points = vtk_to_numpy(obj["mesh"].GetPoints().GetData())
        inner_count = obj.get("inner_count", 0)
        if kind == "thickness":
            triangles = vtk_to_numpy(obj["mesh"].GetPolys().GetData()).reshape(-1, 4)[:, 1:]
            inner_triangles = triangles[np.all(triangles < inner_count, axis=1)]
            target = vtk.vtkPolyData()
            target.SetPoints(obj["mesh"].GetPoints())
            cells = vtk.vtkCellArray()
            cells.SetCells(len(inner_triangles), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(inner_triangles), 3), inner_triangles]).astype(np.int64).ravel(), deep=True))
            target.SetPolys(cells)
            distance = vtk.vtkImplicitPolyDataDistance()
            distance.SetInput(target)
            values = np.array([abs(distance.EvaluateFunction(p)) for p in points])
            editable = np.asarray(obj.get("editable", [True]*len(points)))
            minimum = values[editable].min() if editable.any() else 0
            self.inspection_info.setText(f"편집 영역 최소 두께  {minimum:.3f} mm\n마진 가장자리는 측정 범위에서 제외됩니다.")
            lo, hi, title = 0, max(1.5, obj.get("params", {}).get("wall", .8)*2), "Thickness / mm"
        else:
            antagonist = next((o for o in self.objects.values() if o["role"] == "antagonist"), None)
            if antagonist is None:
                self.statusBar().showMessage("대합치 스캔을 불러와 주세요.", 5000)
                return
            distance = vtk.vtkImplicitPolyDataDistance()
            distance.SetInput(antagonist["mesh"])
            values = np.array([distance.EvaluateFunction(p) for p in points])
            editable = np.asarray(obj.get("editable", [True]*len(points)))
            minimum = values[editable].min() if editable.any() else values.min()
            self.inspection_info.setText(f"대합치 부호 거리 최소  {minimum:.3f} mm\n음수는 내부 방향입니다. 열린 스캔은 부호가 부정확할 수 있습니다.")
            lo, hi, title = -0.3, 2, "Contact / mm"
        array = numpy_to_vtk(values, deep=True)
        array.SetName("Distance_mm")
        obj["mesh"].GetPointData().SetScalars(array)
        lut = vtk.vtkLookupTable()
        lut.SetNumberOfTableValues(256)
        lut.SetRange(lo, hi)
        stops = [(0.85, .16, .19), (.97, .65, .18), (.27, .77, .59), (.18, .48, .88)]
        for i in range(256):
            position = i/255*3
            segment = min(2, int(position))
            fraction = position-segment
            color = np.asarray(stops[segment])*(1-fraction)+np.asarray(stops[segment+1])*fraction
            lut.SetTableValue(i, *color, 1)
        lut.Build()
        mapper = obj["actor"].GetMapper()
        mapper.SetLookupTable(lut)
        mapper.SetScalarRange(lo, hi)
        mapper.ScalarVisibilityOn()
        self.scalar_bar = vtk.vtkScalarBarActor()
        self.scalar_bar.SetLookupTable(lut)
        self.scalar_bar.SetTitle(title)
        self.scalar_bar.SetNumberOfLabels(5)
        self.scalar_bar.SetWidth(0.10)
        self.scalar_bar.SetHeight(0.40)
        self.scalar_bar.SetPosition(.87, .12)
        self.scalar_bar.GetTitleTextProperty().SetFontSize(12)
        self.scalar_bar.GetLabelTextProperty().SetFontSize(11)
        self.renderer.AddViewProp(self.scalar_bar)
        self.color_mode = kind
        self.vtk_widget.GetRenderWindow().Render()

    def inspect_mesh(self):
        obj = self.objects.get(self.active_id)
        if obj is None:
            return
        mesh = obj["mesh"]
        edges = vtk.vtkFeatureEdges()
        edges.SetInputData(mesh)
        edges.BoundaryEdgesOn()
        edges.NonManifoldEdgesOn()
        edges.FeatureEdgesOff()
        edges.ManifoldEdgesOff()
        edges.Update()
        count = edges.GetOutput().GetNumberOfCells()
        volume = vtk.vtkMassProperties()
        volume.SetInputData(mesh)
        volume.Update()
        self.inspection_info.setText(f"{'폐합 메시' if count == 0 else '열린 경계 또는 비다양체 존재'}\n"
                                     f"경계/비다양체 모서리  {count:,}\n표면적  {volume.GetSurfaceArea():.2f} mm²\n"
                                     f"{'체적' if count == 0 else '참고 체적'}  {volume.GetVolume():.2f} mm³\n자가 교차 검사는 포함하지 않습니다.")

    def adapt_opposition(self):
        obj = next((o for o in self.objects.values() if o["role"] == "restoration" and o.get("tooth") == self.current_tooth), None)
        antagonist = next((o for o in self.objects.values() if o["role"] == "antagonist"), None)
        if obj is None or antagonist is None:
            self.statusBar().showMessage("선택 치아의 보철과 대합치 스캔이 필요합니다.", 6000)
            return
        self.push_undo()
        points = vtk_to_numpy(obj["mesh"].GetPoints().GetData())
        axis = np.asarray(obj.get("axis", [0, 0, 1]))
        editable = np.asarray(obj.get("editable", [False]*len(points)))
        tree = vtk.vtkOBBTree()
        tree.SetDataSet(antagonist["mesh"])
        tree.BuildLocator()
        length = np.linalg.norm(np.ptp(vtk_to_numpy(antagonist["mesh"].GetPoints().GetData()), axis=0))+10
        triangles = vtk_to_numpy(obj["mesh"].GetPolys().GetData()).reshape(-1, 4)[:, 1:]
        inner_triangles = triangles[np.all(triangles < obj["inner_count"], axis=1)]
        inner_surface = vtk.vtkPolyData()
        inner_surface.SetPoints(obj["mesh"].GetPoints())
        inner_cells = vtk.vtkCellArray()
        inner_cells.SetCells(len(inner_triangles), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(inner_triangles), 3), inner_triangles]).astype(np.int64).ravel(), deep=True))
        inner_surface.SetPolys(inner_cells)
        inner_distance = vtk.vtkImplicitPolyDataDistance()
        inner_distance.SetInput(inner_surface)
        changed = 0
        hits = vtk.vtkPoints()
        for index in np.flatnonzero(editable):
            point = points[index].copy()
            hits.Reset()
            tree.IntersectWithLine(point+axis*length, point-axis*length, hits, None)
            if hits.GetNumberOfPoints() == 0:
                continue
            candidates = np.asarray([hits.GetPoint(k) for k in range(hits.GetNumberOfPoints())])
            distances = (candidates-point) @ axis
            near = distances[distances > -2.0]
            if len(near) == 0:
                continue
            gap = near.min()
            if gap < self.clearance.value():
                proposed = point-axis*(self.clearance.value()-gap)
                thickness = -inner_distance.EvaluateFunction(proposed)
                if thickness >= max(.3, obj.get("params", {}).get("wall", .8)):
                    points[index] = proposed
                    changed += 1
        obj["mesh"].GetPoints().Modified()
        obj["mesh"].Modified()
        obj["actor"].GetMapper().ScalarVisibilityOff()
        self.dirty = True
        self.vtk_widget.GetRenderWindow().Render()
        self.statusBar().showMessage(f"교합 방향 조정 · {changed:,}개 정점 변경 · 거리맵으로 결과를 확인하세요.", 10000)

    def build_bridge(self):
        first, second = self.bridge_a.currentData(), self.bridge_b.currentData()
        if not first or not second or first == second:
            self.statusBar().showMessage("서로 다른 두 보철을 선택하세요.", 5000)
            return
        a, b = self.objects[first], self.objects[second]
        centers = []
        for obj in [a, b]:
            mesh = obj["mesh"]
            points = vtk_to_numpy(mesh.GetPoints().GetData())
            editable = np.asarray(obj.get("editable", [True]*len(points)))
            centers.append(np.mean(points[editable], axis=0))
        if np.linalg.norm(centers[1]-centers[0]) > 25:
            self.statusBar().showMessage("연결 거리가 25 mm를 넘습니다. 보철 위치를 확인하세요.", 5000)
            return
        line = vtk.vtkLineSource()
        line.SetPoint1(centers[0])
        line.SetPoint2(centers[1])
        line.SetResolution(20)
        tube = vtk.vtkTubeFilter()
        tube.SetInputConnection(line.GetOutputPort())
        tube.SetRadius(self.connector_radius.value())
        tube.SetNumberOfSides(48)
        tube.CappingOn()
        tri = vtk.vtkTriangleFilter()
        tri.SetInputConnection(tube.GetOutputPort())
        clean = vtk.vtkCleanPolyData()
        clean.SetInputConnection(tri.GetOutputPort())
        clean.Update()
        self.push_undo()
        self.add_object(clean.GetOutput(), f"{a['tooth']} ↔ {b['tooth']}", "connector", endpoints=[first, second])
        self.statusBar().showMessage("연결부 생성 완료 · 출력 시 합집합 옵션을 사용하세요.", 7000)

    def repair_mesh(self, operation):
        obj = self.objects.get(self.active_id)
        if obj is None:
            return
        if obj["role"] in ["restoration", "connector"]:
            self.statusBar().showMessage("스캔과 참고 모델에 사용하는 도구입니다.", 5000)
            return
        self.push_undo()
        if operation == "holes":
            process = vtk.vtkFillHolesFilter()
            process.SetHoleSize(self.hole_size.value())
        elif operation == "decimate":
            process = vtk.vtkDecimatePro()
            process.SetTargetReduction(.5)
            process.PreserveTopologyOn()
            process.BoundaryVertexDeletionOff()
        elif operation == "reverse":
            process = vtk.vtkReverseSense()
            process.ReverseCellsOn()
            process.ReverseNormalsOn()
        else:
            process = vtk.vtkCleanPolyData()
        process.SetInputData(obj["mesh"])
        process.Update()
        normals = vtk.vtkPolyDataNormals()
        normals.SetInputConnection(process.GetOutputPort())
        normals.SplittingOff()
        normals.ConsistencyOn()
        normals.AutoOrientNormalsOff()
        normals.Update()
        obj["mesh"].DeepCopy(normals.GetOutput())
        self.dirty = True
        self.refresh_scene()
        self.statusBar().showMessage("메시 처리 완료", 5000)

    def transform_mesh(self):
        obj = self.objects.get(self.active_id)
        if obj is None:
            return
        if obj["role"] in ["restoration", "connector"]:
            self.statusBar().showMessage("스캔과 참고 모델의 정렬에 사용하는 도구입니다.", 5000)
            return
        values = {key: control.value() for key, control in self.transform_controls.items()}
        self.push_undo()
        center = np.asarray(obj["mesh"].GetCenter())
        transform = vtk.vtkTransform()
        transform.PostMultiply()
        transform.Translate(-center)
        transform.Scale(values["scale"], values["scale"], values["scale"])
        transform.RotateX(values["rx"])
        transform.RotateY(values["ry"])
        transform.RotateZ(values["rz"])
        transform.Translate(center+np.asarray([values["tx"], values["ty"], values["tz"]]))
        process = vtk.vtkTransformPolyDataFilter()
        process.SetInputData(obj["mesh"])
        process.SetTransform(transform)
        process.Update()
        obj["mesh"].DeepCopy(process.GetOutput())
        for state in self.teeth.values():
            if state.get("scan_id") == self.active_id:
                state["margin"] = [list(transform.TransformPoint(point)) for point in state["margin"]]
                if "seed" in state:
                    state["seed"] = list(transform.TransformPoint(state["seed"]))
                axis = np.asarray(transform.TransformVector(state["axis"]))
                state["axis"] = (axis/np.linalg.norm(axis)).tolist()
        for key, control in self.transform_controls.items():
            control.setValue(1 if key == "scale" else 0)
        self.update_margin()
        self.refresh_scene()
        self.camera_view("fit")
        self.dirty = True

    def delete_selected(self):
        if self.active_id not in self.objects:
            return
        self.push_undo()
        key = self.active_id
        related = [k for k, obj in self.objects.items() if k == key or key in obj.get("endpoints", [])]
        for item in related:
            self.renderer.RemoveActor(self.objects[item]["actor"])
            del self.objects[item]
        self.active_id = next(iter(self.objects), None)
        self.dirty = True
        self.refresh_scene()

    def save_project(self, checked=False, save_as=False):
        path = str(self.project_path) if self.project_path and not save_as else ""
        if not path:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "프로젝트 저장", self.case_name.text()+".exo", "EXOCADE 프로젝트 (*.exo)")
        if not path:
            return False
        if not path.lower().endswith(".exo"):
            path += ".exo"
        temp = Path(path).with_suffix(".exo.tmp")
        try:
            manifest = {"format": "EXOCADE", "version": 1, "name": self.case_name.text(), "teeth": self.teeth,
                        "current_tooth": self.current_tooth, "objects": {}, "demo_margins": self.demo_margins,
                        "workspace": {"layout_points": self.layout_points,
                                      "active_id": self.active_id,
                                      "bed_mm": [self.printer_bed_x.value(), self.printer_bed_y.value(), self.printer_bed_z.value()],
                                      "print_rotation": [self.print_rx.value(), self.print_ry.value(), self.print_rz.value()],
                                      "print_spacing": self.print_spacing.value(), "output_scope": self.output_scope.currentIndex(),
                                      "output_format": self.output_format.currentIndex(), "self_check": self.output_selfcheck.isChecked()},
                        "camera": {"position": self.renderer.GetActiveCamera().GetPosition(), "focal": self.renderer.GetActiveCamera().GetFocalPoint(),
                                   "up": self.renderer.GetActiveCamera().GetViewUp(), "scale": self.renderer.GetActiveCamera().GetParallelScale()}}
            with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
                for key, obj in self.objects.items():
                    metadata = {k: v for k, v in obj.items() if k not in ["mesh", "actor"]}
                    metadata.update(visible=bool(obj["actor"].GetVisibility()), opacity=obj["actor"].GetProperty().GetOpacity())
                    manifest["objects"][key] = metadata
                    writer = vtk.vtkXMLPolyDataWriter()
                    writer.SetInputData(obj["mesh"])
                    writer.SetDataModeToBinary()
                    writer.WriteToOutputStringOn()
                    if not writer.Write():
                        raise ValueError("메시 데이터를 저장할 수 없습니다.")
                    content = writer.GetOutputString()
                    archive.writestr(f"meshes/{key}.vtp", content.encode("utf-8") if isinstance(content, str) else content)
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
            os.replace(temp, path)
            self.project_path = Path(path)
            self.dirty = False
            self.statusBar().showMessage(f"저장 완료 · {path}", 10000)
            return True
        except Exception as error:
            if temp.exists():
                temp.unlink()
            QtWidgets.QMessageBox.warning(self, "저장 실패", str(error))
            return False

    def open_project(self, checked=False, path_override=None):
        path = path_override or ""
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "프로젝트 열기", "", "EXOCADE 프로젝트 (*.exo)")
        if not path:
            return
        try:
            staged = []
            with zipfile.ZipFile(path) as archive:
                if sum(item.file_size for item in archive.infolist()) > 2_000_000_000:
                    raise ValueError("프로젝트 크기가 2 GB를 초과합니다.")
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                if manifest.get("format") != "EXOCADE" or manifest.get("version") != 1:
                    raise ValueError("지원하지 않는 프로젝트 형식입니다.")
                for key, metadata in manifest["objects"].items():
                    reader = vtk.vtkXMLPolyDataReader()
                    reader.ReadFromInputStringOn()
                    reader.SetInputString(archive.read(f"meshes/{key}.vtp").decode("utf-8"))
                    reader.Update()
                    mesh = vtk.vtkPolyData()
                    mesh.DeepCopy(reader.GetOutput())
                    if mesh.GetNumberOfPolys() == 0:
                        raise ValueError("프로젝트의 메시를 읽을 수 없습니다.")
                    staged.append((key, metadata, mesh))
            if not self.new_case():
                return
            self.teeth = manifest["teeth"]
            self.demo_margins = {int(k): v for k, v in manifest.get("demo_margins", {}).items()}
            workspace = manifest.get("workspace", {})
            self.layout_points = workspace.get("layout_points", [])
            self.update_layout_path()
            for control, value in zip([self.printer_bed_x, self.printer_bed_y, self.printer_bed_z], workspace.get("bed_mm", [140, 80, 160])):
                control.setValue(float(value))
            for control, value in zip([self.print_rx, self.print_ry, self.print_rz], workspace.get("print_rotation", [0, 0, 0])):
                control.setValue(float(value))
            self.print_spacing.setValue(float(workspace.get("print_spacing", 5)))
            self.output_scope.setCurrentIndex(int(workspace.get("output_scope", 0)))
            self.output_format.setCurrentIndex(int(workspace.get("output_format", 0)))
            self.output_selfcheck.setChecked(bool(workspace.get("self_check", True)))
            for key, metadata, mesh in staged:
                name, role = metadata.pop("name"), metadata.pop("role")
                self.add_object(mesh, name, role, key=key, **metadata)
            self.case_name.setText(manifest["name"])
            self.select_tooth(int(manifest.get("current_tooth", 16)))
            if workspace.get("active_id") in self.objects:
                self.active_id = workspace["active_id"]
                self.refresh_scene()
            self.update_print_bed()
            camera = self.renderer.GetActiveCamera()
            saved_camera = manifest.get("camera")
            if saved_camera:
                camera.SetPosition(saved_camera["position"])
                camera.SetFocalPoint(saved_camera["focal"])
                camera.SetViewUp(saved_camera["up"])
                camera.SetParallelScale(saved_camera["scale"])
            self.renderer.ResetCameraClippingRange()
            self.vtk_widget.GetRenderWindow().Render()
            self.project_path = Path(path)
            self.dirty = False
            self.statusBar().showMessage(f"프로젝트 열기 완료 · {Path(path).name}", 10000)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "프로젝트 열기 실패", str(error))

    def analyze_print_mesh(self, mesh, name="제작물"):
        triangulate = vtk.vtkTriangleFilter()
        triangulate.SetInputData(mesh)
        triangulate.PassLinesOff()
        triangulate.PassVertsOff()
        clean = vtk.vtkCleanPolyData()
        clean.SetInputConnection(triangulate.GetOutputPort())
        clean.PointMergingOn()
        clean.ToleranceIsAbsoluteOn()
        clean.SetAbsoluteTolerance(0.0)
        clean.Update()
        result = vtk.vtkPolyData()
        result.DeepCopy(clean.GetOutput())
        if result.GetNumberOfPoints() < 4 or result.GetNumberOfPolys() < 4:
            raise ValueError(f"{name}: 출력할 닫힌 표면이 없습니다.")
        vertices = np.asarray(vtk_to_numpy(result.GetPoints().GetData()), dtype=np.float64)
        if not np.isfinite(vertices).all():
            raise ValueError(f"{name}: 유효하지 않은 정점 좌표가 있습니다.")
        packed = vtk_to_numpy(result.GetPolys().GetData())
        if len(packed) % 4 or not np.all(packed.reshape(-1, 4)[:, 0] == 3):
            raise ValueError(f"{name}: 삼각형으로 변환할 수 없는 면이 있습니다.")
        triangles = np.asarray(packed.reshape(-1, 4)[:, 1:], dtype=np.int64)
        if triangles.min() < 0 or triangles.max() >= len(vertices):
            raise ValueError(f"{name}: 면의 정점 번호가 잘못되었습니다.")
        triangle_points = vertices[triangles]
        cross = np.cross(triangle_points[:, 1]-triangle_points[:, 0], triangle_points[:, 2]-triangle_points[:, 0])
        degenerate = int(np.count_nonzero(np.linalg.norm(cross, axis=1) <= 1e-12))
        if degenerate:
            raise ValueError(f"{name}: 넓이가 없는 삼각형 {degenerate:,}개가 있습니다. 메시를 정리해 주세요.")
        edge_counts = []
        for boundary in [True, False]:
            edges = vtk.vtkFeatureEdges()
            edges.SetInputData(result)
            edges.FeatureEdgesOff()
            edges.ManifoldEdgesOff()
            edges.SetBoundaryEdges(boundary)
            edges.SetNonManifoldEdges(not boundary)
            edges.Update()
            edge_counts.append(edges.GetOutput().GetNumberOfCells())
        if edge_counts[0] or edge_counts[1]:
            raise ValueError(f"{name}: 열린 경계 {edge_counts[0]:,}개, 비다양체 모서리 {edge_counts[1]:,}개가 있습니다.")
        shifted = triangle_points-vertices.mean(axis=0)
        volume = float(np.einsum("ij,ij->i", shifted[:, 0], np.cross(shifted[:, 1], shifted[:, 2])).sum()/6)
        if volume <= 1e-8:
            raise ValueError(f"{name}: 체적 또는 면 방향이 잘못되었습니다. 법선과 메시 상태를 확인해 주세요.")
        selfcheck = self.output_selfcheck.isChecked() if hasattr(self, "output_selfcheck") else True
        pairs_checked = 0
        if selfcheck:
            import hashlib

            digest = hashlib.sha256(b"EXOCADE triangle intersection v2")
            digest.update(triangle_points.tobytes())
            signature = digest.hexdigest()
            if not hasattr(self, "_intersection_cache"):
                self._intersection_cache = {}
            cached = self._intersection_cache.get(signature)
            if cached is not None:
                intersection = cached["pair"]
                pairs_checked = cached["pairs_checked"]
            else:
                minimum = triangle_points.min(axis=1)
                maximum = triangle_points.max(axis=1)
                centers = triangle_points.mean(axis=1)
                shrunk = centers[:, None, :]+(triangle_points-centers[:, None, :])*(1-1e-7)
                normals = cross/np.linalg.norm(cross, axis=1)[:, None]
                epsilon = max(1e-12, float(np.linalg.norm(np.ptp(vertices, axis=0)))*1e-12)
                intersection_mesh = vtk.vtkPolyData()
                intersection_mesh.SetPoints(result.GetPoints())
                intersection_mesh.SetPolys(result.GetPolys())
                locator = vtk.vtkStaticCellLocator()
                locator.SetDataSet(intersection_mesh)
                locator.BuildLocator()
                candidate_ids = vtk.vtkIdList()
                intersection = None
                for index, triangle in enumerate(triangle_points):
                    if index % 2048 == 0:
                        self.statusBar().showMessage(f"{name} · 자가 교차 검사 {index+1:,} / {len(triangles):,}면")
                        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
                    bounds = np.column_stack([minimum[index]-epsilon, maximum[index]+epsilon]).ravel()
                    candidate_ids.Reset()
                    locator.FindCellsWithinBounds(bounds, candidate_ids)
                    candidates = np.fromiter((candidate_ids.GetId(j) for j in range(candidate_ids.GetNumberOfIds())), dtype=np.int64)
                    candidates = candidates[candidates > index]
                    if not len(candidates):
                        continue
                    overlap = np.all(maximum[candidates]+epsilon >= minimum[index], axis=1) & np.all(minimum[candidates]-epsilon <= maximum[index], axis=1)
                    candidates = candidates[overlap]
                    if not len(candidates):
                        continue
                    first_plane = np.einsum("mki,i->mk", triangle_points[candidates]-triangle[0], normals[index])
                    opposite_plane = np.einsum("mki,mi->mk", triangle[None, :, :]-triangle_points[candidates, 0, None, :], normals[candidates])
                    possible = ~((first_plane.min(axis=1) > epsilon) | (first_plane.max(axis=1) < -epsilon) |
                                 (opposite_plane.min(axis=1) > epsilon) | (opposite_plane.max(axis=1) < -epsilon))
                    candidates = candidates[possible]
                    if not len(candidates):
                        continue
                    shared = np.any(triangles[candidates, :, None] == triangles[index][None, None, :], axis=(1, 2))
                    for other, adjacent in zip(candidates, shared):
                        first = shrunk[index] if adjacent else triangle
                        second = shrunk[other] if adjacent else triangle_points[other]
                        pairs_checked += 1
                        if vtk.vtkTriangle.TrianglesIntersect(first[0], first[1], first[2], second[0], second[1], second[2]):
                            first_distances = (second-first[0]) @ normals[index]
                            second_distances = (first-second[0]) @ normals[other]
                            if first_distances.min() > epsilon or first_distances.max() < -epsilon or second_distances.min() > epsilon or second_distances.max() < -epsilon:
                                continue
                            first_edges = np.roll(first, -1, axis=0)-first
                            second_edges = np.roll(second, -1, axis=0)-second
                            separating_axes = np.vstack([normals[index], normals[other],
                                np.cross(first_edges[:, None, :], second_edges[None, :, :]).reshape(-1, 3),
                                np.cross(normals[index], first_edges), np.cross(normals[other], second_edges)])
                            first_projection = separating_axes @ (first-first[0]).T
                            second_projection = separating_axes @ (second-first[0]).T
                            rounding = np.finfo(np.float64).eps*128*np.maximum(np.maximum(np.max(np.abs(first_projection), axis=1), np.max(np.abs(second_projection), axis=1)), 1e-30)
                            if np.any((first_projection.max(axis=1) < second_projection.min(axis=1)-rounding) |
                                      (second_projection.max(axis=1) < first_projection.min(axis=1)-rounding)):
                                continue
                            intersection = (index, int(other))
                            break
                    if intersection is not None:
                        break
                self._intersection_cache[signature] = {"pair": intersection, "pairs_checked": pairs_checked}
                while len(self._intersection_cache) > 32:
                    self._intersection_cache.pop(next(iter(self._intersection_cache)))
            if intersection is not None:
                raise ValueError(f"{name}: 삼각형 {intersection[0]+1:,}번과 {intersection[1]+1:,}번이 서로 관통하거나 겹칩니다. 형상을 수정한 뒤 출력해 주세요.")
        solid = manifold3d.Manifold(manifold3d.Mesh64(np.ascontiguousarray(vertices), np.ascontiguousarray(triangles, dtype=np.uint64)))
        if solid.status() != manifold3d.Error.NoError or solid.is_empty():
            raise ValueError(f"{name}: 출력용 닫힌 메시로 해석할 수 없습니다. ({solid.status()})")
        components = vtk.vtkPolyDataConnectivityFilter()
        components.SetInputData(result)
        components.SetExtractionModeToAllRegions()
        components.Update()
        bounds = np.asarray(result.GetBounds(), dtype=float)
        if not np.isfinite(bounds).all() or np.any(bounds[1::2] <= bounds[::2]):
            raise ValueError(f"{name}: 출력 모델의 크기가 잘못되었습니다.")
        report = {"vertices": len(vertices), "triangles": len(triangles), "boundary_edges": edge_counts[0],
                  "nonmanifold_edges": edge_counts[1], "components": components.GetNumberOfExtractedRegions(),
                  "volume_mm3": volume, "bounds_mm": bounds.tolist(), "self_intersection_checked": bool(selfcheck),
                  "intersection_pairs_tested": pairs_checked}
        return result, report, solid

    def printable_parts(self, selected=False, force_original=False):
        import hashlib

        roles = {"restoration", "connector", "model", "appliance", "die", "pontic", "implant", "bar", "denture", "gingiva", "part", "segmentation"}
        scope = 1 if selected else (self.output_scope.currentIndex() if hasattr(self, "output_scope") else 0)
        originals = {key: obj for key, obj in self.objects.items() if obj["role"] in roles}
        prepared = {key: obj for key, obj in self.objects.items() if obj["role"] == "print"}
        if scope == 1:
            if self.active_id not in self.objects:
                raise ValueError("출력할 오브젝트를 선택해 주세요.")
            chosen = self.objects[self.active_id]
            if chosen["role"] == "print" and force_original:
                chosen_ids = chosen.get("source_ids", [])
                entries = {key: originals[key] for key in chosen_ids if key in originals}
            else:
                entries = {self.active_id: chosen}
        elif prepared and not force_original:
            prepared_sources = set(source for obj in prepared.values() for source in obj.get("source_ids", []))
            if prepared_sources != set(originals):
                raise ValueError("출력 배치에 포함되지 않은 제작물이 있습니다. 전체 제작물을 다시 배치하거나 선택 출력으로 저장해 주세요.")
            entries = prepared
        else:
            entries = originals
        if not entries:
            raise ValueError("출력할 제작물이 없습니다. 보철·모델·장치를 먼저 생성해 주세요.")
        for key, obj in entries.items():
            if obj["role"] == "print":
                if all(hasattr(self, name) for name in ["printer_bed_x", "printer_bed_y", "printer_bed_z"]):
                    bed = np.asarray([self.printer_bed_x.value(), self.printer_bed_y.value(), self.printer_bed_z.value()], dtype=float)
                else:
                    bed = np.asarray(obj.get("print_bed_mm", []), dtype=float)
                bounds = np.asarray(obj["mesh"].GetBounds(), dtype=float).reshape(3, 2)
                if bed.shape != (3,) or not np.isfinite(bed).all() or np.any(bed <= 0):
                    raise ValueError("현재 출력판 크기를 확인하고 출력 준비를 다시 실행해 주세요.")
                if not np.isfinite(bounds).all() or np.any(bounds[:, 0] < -1e-5) or np.any(bounds[:, 1] > bed+1e-5):
                    raise ValueError(f"{obj['name']}이 현재 출력 공간 {bed[0]:g} × {bed[1]:g} × {bed[2]:g} mm를 벗어납니다. 출력 준비를 다시 실행해 주세요.")
                signatures = obj.get("source_signatures", {})
                if not signatures:
                    raise ValueError("이 출력 사본은 원본 정보를 확인할 수 없습니다. 원본을 선택하고 다시 배치해 주세요.")
                for original_key, expected in signatures.items():
                    original = self.objects.get(original_key)
                    if original is None:
                        raise ValueError("출력 사본의 원본이 삭제되었습니다. 출력 준비를 다시 실행해 주세요.")
                    digest = hashlib.sha256()
                    digest.update(vtk_to_numpy(original["mesh"].GetPoints().GetData()).tobytes())
                    digest.update(vtk_to_numpy(original["mesh"].GetPolys().GetData()).tobytes())
                    if digest.hexdigest() != expected:
                        raise ValueError("배치 후 원본 형상이 변경되었습니다. 출력 준비를 다시 실행해 주세요.")
                    if original["role"] == "restoration" and original.get("inner_count"):
                        state = self.teeth.get(str(original.get("tooth")), {})
                        if state.get("generated_margin") != state.get("margin") or not np.allclose(state.get("axis", [0, 0, 1]), original.get("axis", [0, 0, 1])):
                            raise ValueError(f"{original.get('tooth')}번의 마진 또는 삽입축이 변경되었습니다. 보철 생성과 출력 준비를 다시 실행해 주세요.")
            elif obj["role"] not in roles:
                if scope == 1:
                    raise ValueError("스캔·대합치·참고 모델은 출력용 제작물로 변환한 뒤 내보내 주세요.")
            elif obj["role"] == "restoration" and obj.get("inner_count"):
                state = self.teeth.get(str(obj.get("tooth")), {})
                if state.get("generated_margin") != state.get("margin") or not np.allclose(state.get("axis", [0, 0, 1]), obj.get("axis", [0, 0, 1])):
                    raise ValueError(f"{obj.get('tooth')}번의 마진 또는 삽입축이 변경되었습니다. 보철을 다시 생성한 뒤 내보내 주세요.")
        merge = self.merge_export.isChecked() if hasattr(self, "merge_export") else True
        if scope == 1 and merge and not any(obj["role"] == "print" for obj in entries.values()):
            connected = set(entries)
            changed = True
            while changed:
                changed = False
                for connector_key, connector in originals.items():
                    if connector["role"] != "connector":
                        continue
                    endpoints = set(connector.get("endpoints", []))
                    if connector_key in connected or endpoints.intersection(connected):
                        additional = endpoints.union({connector_key})-connected
                        if additional:
                            connected.update(additional)
                            changed = True
            missing = connected-set(originals)
            if missing:
                raise ValueError("브릿지 연결 대상이 삭제되었습니다. 연결부를 다시 만들어 주세요.")
            entries = {key: originals[key] for key in connected}
        groups = [{key} for key in entries]
        connectors = [key for key, obj in entries.items() if obj["role"] == "connector"]
        if connectors and not merge:
            raise ValueError("연결부가 있는 제작물은 브릿지 합집합 옵션을 켠 뒤 출력해 주세요.")
        for key in connectors:
            endpoints = set(entries[key].get("endpoints", []))
            if not endpoints or not endpoints.issubset(entries):
                raise ValueError("브릿지 연결 대상이 없습니다. 연결부를 다시 만들어 주세요.")
            related = endpoints.union({key})
            combined = set(related)
            remaining = []
            for group in groups:
                if group.intersection(related):
                    combined.update(group)
                else:
                    remaining.append(group)
            groups = remaining+[combined]
        groups.sort(key=lambda group: min(group))
        parts = []
        for group in groups:
            group_entries = [(key, entries[key]) for key in sorted(group)]
            for key, obj in group_entries:
                if obj["role"] == "restoration" and obj.get("inner_count"):
                    state = self.teeth.get(str(obj.get("tooth")), {})
                    if state.get("generated_margin") != state.get("margin") or not np.allclose(state.get("axis", [0, 0, 1]), obj.get("axis", [0, 0, 1])):
                        raise ValueError(f"{obj.get('tooth')}번의 마진 또는 삽입축이 변경되었습니다. 보철을 다시 생성한 뒤 내보내 주세요.")
            teeth = sorted({int(obj["tooth"]) for key, obj in group_entries if obj.get("tooth") is not None})
            label = "브릿지 " + "-".join(map(str, teeth)) if len(group) > 1 else group_entries[0][1]["name"]
            if len(group) == 1:
                mesh, report, solid = self.analyze_print_mesh(group_entries[0][1]["mesh"], label)
            else:
                solids = []
                bounds = []
                for key, obj in group_entries:
                    mesh, report, solid = self.analyze_print_mesh(obj["mesh"], obj["name"])
                    solids.append(solid)
                    bounds.append(np.asarray(mesh.GetBounds()).reshape(3, 2))
                solid = manifold3d.Manifold.batch_boolean(solids, manifold3d.OpType.Add)
                all_bounds = np.asarray(bounds)
                depth = np.linalg.norm(all_bounds[:, :, 1].max(axis=0)-all_bounds[:, :, 0].min(axis=0))+5
                cavities = []
                for key, obj in group_entries:
                    if obj["role"] != "restoration" or not obj.get("inner_count"):
                        continue
                    count = int(obj["inner_count"])
                    ring_count = int(obj.get("ring_points", 96))
                    vertices = np.asarray(vtk_to_numpy(obj["mesh"].GetPoints().GetData()), dtype=np.float64)
                    if ring_count < 3 or count <= ring_count or count > len(vertices):
                        raise ValueError("크라운 내면 정보가 잘못되었습니다. 보철을 다시 생성해 주세요.")
                    axis = np.asarray(obj.get("axis", [0, 0, 1]), dtype=float)
                    if np.linalg.norm(axis) < 1e-8:
                        raise ValueError("크라운 삽입축 정보가 잘못되었습니다.")
                    axis /= np.linalg.norm(axis)
                    boundary = vertices[count-ring_count:count]
                    lower = boundary-axis*depth
                    cavity_vertices = np.vstack([vertices[:count], lower, lower.mean(axis=0)])
                    faces = vtk_to_numpy(obj["mesh"].GetPolys().GetData()).reshape(-1, 4)[:, 1:]
                    cavity_faces = faces[np.all(faces < count, axis=1)][:, [0, 2, 1]].tolist()
                    for j in range(ring_count):
                        k = (j+1)%ring_count
                        a, b, c, d = count-ring_count+j, count-ring_count+k, count+j, count+k
                        cavity_faces.extend([[a, c, d], [a, d, b], [len(cavity_vertices)-1, d, c]])
                    cavity = manifold3d.Manifold(manifold3d.Mesh64(np.ascontiguousarray(cavity_vertices), np.ascontiguousarray(cavity_faces, dtype=np.uint64)))
                    if cavity.status() != manifold3d.Error.NoError or cavity.is_empty():
                        raise ValueError("크라운 내부 공간을 보존할 수 없습니다. 보철을 다시 생성해 주세요.")
                    cavities.append(cavity)
                if cavities:
                    solid = manifold3d.Manifold.batch_boolean([solid]+cavities, manifold3d.OpType.Subtract)
                if solid.status() != manifold3d.Error.NoError or solid.is_empty():
                    raise ValueError("브릿지 합집합에 실패했습니다. 연결부와 보철을 확인해 주세요.")
                result = solid.to_mesh64()
                points = vtk.vtkPoints()
                points.SetData(numpy_to_vtk(np.asarray(result.vert_properties[:, :3]).copy(), deep=True))
                faces = np.asarray(result.tri_verts, dtype=np.int64)
                cells = vtk.vtkCellArray()
                cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).astype(np.int64).ravel(), deep=True))
                data = vtk.vtkPolyData()
                data.SetPoints(points)
                data.SetPolys(cells)
                mesh, report, solid = self.analyze_print_mesh(data, label)
                if len(solid.decompose()) != 1:
                    raise ValueError("브릿지가 하나로 연결되지 않았습니다. 연결부 위치와 반경을 조정해 주세요.")
            source_ids = []
            for key, obj in group_entries:
                source_ids.extend(obj.get("source_ids", [key]) if obj["role"] == "print" else [key])
            parts.append({"name": label, "mesh": mesh, "report": report, "solid": solid,
                          "source_ids": sorted(set(source_ids)), "teeth": teeth, "prepared": all(obj["role"] == "print" for key, obj in group_entries)})
        return parts

    def export_mesh(self, checked=False, selected=False):
        import re
        import struct
        import xml.etree.ElementTree as ET

        if getattr(self, "busy", False):
            return False
        scope = 1 if selected else (self.output_scope.currentIndex() if hasattr(self, "output_scope") else 0)
        format_text = self.output_format.currentText() if hasattr(self, "output_format") else "STL"
        extension = ".3mf" if "3MF" in format_text.upper() else ".stl"
        default_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", self.case_name.text()).strip(" .") or "EXOCADE"
        destination = ""
        if scope == 2:
            destination = QtWidgets.QFileDialog.getExistingDirectory(self, "치아·제작물별 파일 저장 폴더")
        else:
            destination, _ = QtWidgets.QFileDialog.getSaveFileName(self, "출력용 모델 저장 · mm", default_name+extension,
                "3MF (*.3mf)" if extension == ".3mf" else "바이너리 STL (*.stl)")
        if not destination:
            return False
        self.busy = True
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        temporary_paths = []
        staged = []
        created_directory = None
        try:
            self.statusBar().showMessage("출력 메시 구조와 연결부를 검사하고 있습니다…")
            QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            parts = self.printable_parts(selected=selected)
            if len(parts) > 1:
                for index, first in enumerate(parts):
                    first_bounds = np.asarray(first["mesh"].GetBounds()).reshape(3, 2)
                    for second in parts[index+1:]:
                        second_bounds = np.asarray(second["mesh"].GetBounds()).reshape(3, 2)
                        if np.all(np.minimum(first_bounds[:, 1], second_bounds[:, 1])-np.maximum(first_bounds[:, 0], second_bounds[:, 0]) > 1e-6):
                            overlap = first["solid"] ^ second["solid"]
                            if not overlap.is_empty() and overlap.volume() > 1e-6:
                                raise ValueError("서로 다른 제작물이 겹칩니다. 출력 준비에서 제작물을 배치한 뒤 내보내 주세요.")
            if scope == 2:
                folder = Path(destination)/(default_name+"_"+uuid.uuid4().hex[:8])
                folder.mkdir()
                created_directory = folder
                jobs = []
                for index, part in enumerate(parts, 1):
                    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", part["name"]).strip(" .") or f"제작물_{index}"
                    jobs.append((folder/f"{index:02d}_{name[:100]}{extension}", [part]))
            else:
                path = Path(destination)
                if path.suffix.lower() != extension:
                    path = path.with_suffix(extension)
                jobs = [(path, parts)]
            all_reports = []
            for path, job_parts in jobs:
                temporary = path.parent/("."+path.name+"."+uuid.uuid4().hex+".tmp")
                temporary_paths.append(temporary)
                if extension == ".stl":
                    append = vtk.vtkAppendPolyData()
                    for part in job_parts:
                        append.AddInputData(part["mesh"])
                    append.Update()
                    data, report, solid = self.analyze_print_mesh(append.GetOutput(), path.name)
                    original_vertices = np.asarray(vtk_to_numpy(data.GetPoints().GetData()), dtype=np.float64)
                    coordinate_max = max(1., float(np.max(np.abs(original_vertices))))
                    if not np.isfinite(original_vertices.astype(np.float32)).all():
                        raise ValueError("STL의 32비트 좌표 범위를 벗어났습니다. 출력 준비에서 배치해 주세요.")
                    deviation_limit = .001
                    quantization_bound = float(np.sqrt(3))*float(np.spacing(np.float32(coordinate_max)))/2
                    maximum_tolerance = deviation_limit-quantization_bound
                    original_solid = solid.as_original()
                    minimum_tolerance = max(1e-5, coordinate_max*np.finfo(np.float32).eps*2, original_solid.get_tolerance())
                    if maximum_tolerance <= 0 or original_solid.get_tolerance() > maximum_tolerance:
                        raise ValueError("현재 좌표에서는 STL 정밀도를 0.001 mm 이내로 유지할 수 없습니다. 출력판에 배치하거나 3MF로 저장해 주세요.")
                    tolerance = min(minimum_tolerance, maximum_tolerance)
                    original_components = len(solid.decompose())
                    cleanup_error = ""
                    for cleanup_attempt in range(12):
                        self.statusBar().showMessage(f"STL 미세면 정리 · 허용오차 {tolerance:.6g} mm · float32 좌표 검증")
                        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
                        simplified = original_solid.simplify(tolerance)
                        if simplified.status() != manifold3d.Error.NoError or simplified.is_empty():
                            cleanup_error = "미세면 정리 후 닫힌 메시를 유지하지 못했습니다."
                        else:
                            result = simplified.to_mesh64()
                            simplified_vertices = np.asarray(result.vert_properties[:, :3], dtype=np.float64)
                            quantized = simplified_vertices.astype(np.float32).astype(np.float64)
                            quantization_error = float(np.max(np.linalg.norm(quantized-simplified_vertices, axis=1)))
                            if not np.isfinite(quantized).all() or tolerance+quantization_error > deviation_limit+1e-12:
                                cleanup_error = "STL 양자화 오차가 0.001 mm 한도를 초과합니다."
                            else:
                                welded_vertices, remap = np.unique(quantized, axis=0, return_inverse=True)
                                welded_faces = remap[np.asarray(result.tri_verts, dtype=np.int64)]
                                face_points = welded_vertices[welded_faces]
                                valid = ((welded_faces[:, 0] != welded_faces[:, 1]) & (welded_faces[:, 1] != welded_faces[:, 2]) &
                                         (welded_faces[:, 2] != welded_faces[:, 0]) &
                                         (np.linalg.norm(np.cross(face_points[:, 1]-face_points[:, 0], face_points[:, 2]-face_points[:, 0]), axis=1) > 0))
                                welded_faces = welded_faces[valid]
                                points = vtk.vtkPoints()
                                points.SetData(numpy_to_vtk(welded_vertices, deep=True))
                                cells = vtk.vtkCellArray()
                                cells.SetCells(len(welded_faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(welded_faces), 3), welded_faces]).astype(np.int64).ravel(), deep=True))
                                candidate = vtk.vtkPolyData()
                                candidate.SetPoints(points)
                                candidate.SetPolys(cells)
                                try:
                                    candidate_data, candidate_report, candidate_solid = self.analyze_print_mesh(candidate, path.name+" · STL 좌표")
                                    if len(candidate_solid.decompose()) != original_components:
                                        raise ValueError("미세면 정리 과정에서 분리된 제작물의 개수가 달라졌습니다.")
                                    data, report, solid = candidate_data, candidate_report, candidate_solid
                                    report.update(stl_cleanup_tolerance_mm=float(tolerance),
                                                  stl_float32_max_vertex_error_mm=quantization_error,
                                                  stl_max_surface_deviation_bound_mm=float(tolerance+quantization_error),
                                                  stl_allowed_max_deviation_mm=deviation_limit)
                                    break
                                except ValueError as error:
                                    cleanup_error = str(error)
                        if tolerance >= maximum_tolerance*(1-1e-12):
                            raise ValueError("STL 변환을 0.001 mm 이내에서 안정화하지 못했습니다. 3MF를 사용하거나 형상을 수정해 주세요.\n"+cleanup_error)
                        tolerance = min(tolerance*2, maximum_tolerance)
                    else:
                        raise ValueError("STL 미세면 정리를 완료하지 못했습니다.\n"+cleanup_error)
                    vertices = np.asarray(vtk_to_numpy(data.GetPoints().GetData()), dtype=np.float64)
                    faces = vtk_to_numpy(data.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
                    triangle_points = vertices[faces]
                    normals = np.cross(triangle_points[:, 1]-triangle_points[:, 0], triangle_points[:, 2]-triangle_points[:, 0])
                    normals /= np.linalg.norm(normals, axis=1)[:, None]
                    records = np.zeros(len(faces), dtype=np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")]))
                    records["normal"] = normals
                    records["vertices"] = triangle_points
                    if not np.isfinite(records["vertices"]).all() or not np.isfinite(records["normal"]).all():
                        raise ValueError("STL의 32비트 좌표 범위를 벗어났습니다. 출력 준비에서 배치해 주세요.")
                    with temporary.open("xb") as output:
                        header = f"EXOCADE STL mm | cleanup {tolerance:.6g}mm | deviation <=0.001mm".encode("ascii")
                        output.write(header[:80].ljust(80, b"\x00"))
                        output.write(struct.pack("<I", len(faces)))
                        output.write(records.tobytes())
                        output.flush()
                        os.fsync(output.fileno())
                    with temporary.open("rb") as saved:
                        header = saved.read(84)
                    if len(header) != 84 or struct.unpack("<I", header[80:84])[0] != len(faces) or temporary.stat().st_size != 84+50*len(faces):
                        raise ValueError("STL 파일의 삼각형 개수 또는 길이가 일치하지 않습니다.")
                    reader = vtk.vtkSTLReader()
                    reader.SetFileName(str(temporary))
                    reader.MergingOn()
                    reader.Update()
                    restored, restored_report, restored_solid = self.analyze_print_mesh(reader.GetOutput(), path.name)
                    scale = max(1., float(np.max(np.abs(report["bounds_mm"]))))
                    if restored_report["triangles"] != report["triangles"] or not np.allclose(restored.GetBounds(), data.GetBounds(), rtol=0, atol=max(1e-5, scale*2e-7)):
                        raise ValueError("저장한 STL을 다시 읽었을 때 형상 크기 또는 면 개수가 달라졌습니다.")
                    all_reports.append(report)
                else:
                    namespace = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
                    ET.register_namespace("", namespace)
                    model = ET.Element("{"+namespace+"}model", {"unit": "millimeter", "{http://www.w3.org/XML/1998/namespace}lang": "ko-KR"})
                    ET.SubElement(model, "{"+namespace+"}metadata", {"name": "Application"}).text = "EXOCADE"
                    ET.SubElement(model, "{"+namespace+"}metadata", {"name": "Title"}).text = self.case_name.text()
                    resources = ET.SubElement(model, "{"+namespace+"}resources")
                    build = ET.SubElement(model, "{"+namespace+"}build")
                    expected = []
                    for object_id, part in enumerate(job_parts, 1):
                        data, report, solid = self.analyze_print_mesh(part["mesh"], part["name"])
                        vertices = np.asarray(vtk_to_numpy(data.GetPoints().GetData()), dtype=np.float64)
                        faces = vtk_to_numpy(data.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
                        object_element = ET.SubElement(resources, "{"+namespace+"}object", {"id": str(object_id), "type": "model", "name": part["name"]})
                        mesh_element = ET.SubElement(object_element, "{"+namespace+"}mesh")
                        vertex_element = ET.SubElement(mesh_element, "{"+namespace+"}vertices")
                        for point in vertices:
                            ET.SubElement(vertex_element, "{"+namespace+"}vertex", {axis: format(float(value), ".17g") for axis, value in zip(["x", "y", "z"], point)})
                        face_element = ET.SubElement(mesh_element, "{"+namespace+"}triangles")
                        for face in faces:
                            ET.SubElement(face_element, "{"+namespace+"}triangle", {key: str(int(value)) for key, value in zip(["v1", "v2", "v3"], face)})
                        ET.SubElement(build, "{"+namespace+"}item", {"objectid": str(object_id)})
                        expected.append((vertices, faces))
                        all_reports.append(report)
                    types_namespace = "http://schemas.openxmlformats.org/package/2006/content-types"
                    content_types = ET.Element("{"+types_namespace+"}Types")
                    ET.SubElement(content_types, "{"+types_namespace+"}Default", {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"})
                    ET.SubElement(content_types, "{"+types_namespace+"}Default", {"Extension": "model", "ContentType": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"})
                    relation_namespace = "http://schemas.openxmlformats.org/package/2006/relationships"
                    relationships = ET.Element("{"+relation_namespace+"}Relationships")
                    ET.SubElement(relationships, "{"+relation_namespace+"}Relationship", {"Id": "rel0", "Type": "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel", "Target": "/3D/3dmodel.model"})
                    with zipfile.ZipFile(temporary, "x", zipfile.ZIP_DEFLATED, compresslevel=4) as archive:
                        archive.writestr("[Content_Types].xml", ET.tostring(content_types, encoding="utf-8", xml_declaration=True))
                        archive.writestr("_rels/.rels", ET.tostring(relationships, encoding="utf-8", xml_declaration=True))
                        archive.writestr("3D/3dmodel.model", ET.tostring(model, encoding="utf-8", xml_declaration=True))
                    with zipfile.ZipFile(temporary) as archive:
                        if archive.testzip() is not None:
                            raise ValueError("3MF 압축 데이터가 손상되었습니다.")
                        saved_types = ET.fromstring(archive.read("[Content_Types].xml"))
                        if not any(element.get("Extension") == "model" and element.get("ContentType") == "application/vnd.ms-package.3dmanufacturing-3dmodel+xml" for element in saved_types):
                            raise ValueError("3MF 모델 콘텐츠 형식이 잘못되었습니다.")
                        saved_relationships = ET.fromstring(archive.read("_rels/.rels"))
                        if not any(element.get("Type") == "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" and element.get("Target") == "/3D/3dmodel.model" for element in saved_relationships):
                            raise ValueError("3MF 모델 연결 정보가 잘못되었습니다.")
                        saved_model = ET.fromstring(archive.read("3D/3dmodel.model"))
                    if saved_model.tag != "{"+namespace+"}model" or saved_model.get("unit") != "millimeter":
                        raise ValueError("3MF의 네임스페이스 또는 길이 단위가 잘못되었습니다.")
                    saved_objects = saved_model.findall("{"+namespace+"}resources/{"+namespace+"}object")
                    saved_items = saved_model.findall("{"+namespace+"}build/{"+namespace+"}item")
                    if len(saved_objects) != len(expected) or len(saved_items) != len(expected):
                        raise ValueError("3MF의 제작물 개수가 일치하지 않습니다.")
                    for object_id, (object_element, (expected_vertices, expected_faces)) in enumerate(zip(saved_objects, expected), 1):
                        saved_vertices = np.asarray([[float(vertex.get(axis, "nan")) for axis in ["x", "y", "z"]] for vertex in object_element.findall("{"+namespace+"}mesh/{"+namespace+"}vertices/{"+namespace+"}vertex")])
                        saved_faces = np.asarray([[int(face.get(key, "-1")) for key in ["v1", "v2", "v3"]] for face in object_element.findall("{"+namespace+"}mesh/{"+namespace+"}triangles/{"+namespace+"}triangle")], dtype=np.int64)
                        if object_element.get("id") != str(object_id) or saved_items[object_id-1].get("objectid") != str(object_id) or saved_vertices.shape != expected_vertices.shape or not np.array_equal(saved_vertices, expected_vertices) or not np.array_equal(saved_faces, expected_faces):
                            raise ValueError("3MF를 다시 읽었을 때 정점·삼각형 또는 제작물 참조가 달라졌습니다.")
                staged.append((temporary, path))
            for temporary, path in staged:
                os.replace(temporary, path)
            self.last_export_paths = [str(path.resolve()) for temporary, path in staged]
            self.last_export_reports = copy.deepcopy(all_reports)
            total_triangles = sum(report["triangles"] for report in all_reports)
            total_volume = sum(report["volume_mm3"] for report in all_reports)
            message = f"{len(staged)}개 파일 저장·재읽기 확인 완료 · {total_triangles:,} 삼각형 · {total_volume:.2f} mm³"
            if hasattr(self, "print_info"):
                cleanup_message = ""
                if extension == ".stl":
                    maximum_change = max(report.get("stl_max_surface_deviation_bound_mm", 0) for report in all_reports)
                    cleanup_message = f"\nSTL 미세면 정리·좌표 오차 한도 0.001 mm · 이번 출력 상한 {maximum_change:.6g} mm"
                self.print_info.setText(message+"\n길이 단위: mm"+cleanup_message+"\n서포트·레이어·노광 설정은 슬라이서에서 적용하세요.")
            if hasattr(self, "inspection_info"):
                intersection_message = "자가 교차 검사 통과" if all(report["self_intersection_checked"] for report in all_reports) else "자가 교차 검사는 실행하지 않았습니다."
                self.inspection_info.setText(message+"\n메시 구조 검사 통과 · "+intersection_message)
            self.statusBar().showMessage("내보내기 완료 · "+self.last_export_paths[0], 20000)
            if hasattr(self, "auto_open_slicer") and self.auto_open_slicer.isChecked():
                self.open_slicer()
            return True
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "출력 파일 저장 실패", str(error))
            return False
        finally:
            for temporary in temporary_paths:
                if temporary.exists():
                    temporary.unlink()
            if created_directory is not None and created_directory.exists() and not any(created_directory.iterdir()):
                created_directory.rmdir()
            QtWidgets.QApplication.restoreOverrideCursor()
            self.busy = False

    def prepare_print_layout(self):
        import hashlib

        if getattr(self, "busy", False):
            return
        self.busy = True
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            parts = self.printable_parts(force_original=True)
            bed = np.array([self.printer_bed_x.value(), self.printer_bed_y.value(), self.printer_bed_z.value()], dtype=float)
            gap = self.print_spacing.value()
            angle_x, angle_y, angle_z = np.radians([self.print_rx.value(), self.print_ry.value(), self.print_rz.value()])
            cx, sx, cy, sy, cz, sz = np.cos(angle_x), np.sin(angle_x), np.cos(angle_y), np.sin(angle_y), np.cos(angle_z), np.sin(angle_z)
            rotation_x = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
            rotation_y = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
            rotation_z = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
            rotation = rotation_z @ rotation_y @ rotation_x
            arranged = []
            for part in parts:
                vertices = np.asarray(vtk_to_numpy(part["mesh"].GetPoints().GetData()), dtype=np.float64)
                vertices = (vertices-vertices.mean(axis=0)) @ rotation.T
                minimum, maximum = vertices.min(axis=0), vertices.max(axis=0)
                size = maximum-minimum
                if np.any(size+np.array([gap*2, gap*2, 0]) > bed+1e-6):
                    raise ValueError(f"{part['name']}의 크기 {size[0]:.1f} × {size[1]:.1f} × {size[2]:.1f} mm가 출력 공간을 벗어납니다.")
                arranged.append({"part": part, "vertices": vertices-minimum, "size": size})
            arranged.sort(key=lambda item: (-item["size"][1], -item["size"][0]))
            cursor_x, cursor_y, row_height = gap, gap, 0.0
            maximum_x, maximum_y = 0.0, 0.0
            for item in arranged:
                width, depth, height = item["size"]
                if cursor_x+width+gap > bed[0]+1e-6:
                    cursor_x, cursor_y, row_height = gap, cursor_y+row_height+gap, 0.0
                if cursor_y+depth+gap > bed[1]+1e-6:
                    raise ValueError("제작물을 출력판에 모두 배치할 수 없습니다. 간격을 줄이거나 나누어 출력해 주세요.")
                item["vertices"] += np.array([cursor_x, cursor_y, 0])
                maximum_x = max(maximum_x, cursor_x+width)
                maximum_y = max(maximum_y, cursor_y+depth)
                cursor_x += width+gap
                row_height = max(row_height, depth)
            offset = np.array([(bed[0]-(maximum_x+gap))/2, (bed[1]-(maximum_y+gap))/2, 0])
            staged = []
            for item in arranged:
                part = item["part"]
                data = vtk.vtkPolyData()
                data.DeepCopy(part["mesh"])
                points = vtk.vtkPoints()
                points.SetData(numpy_to_vtk(item["vertices"]+offset, deep=True))
                data.SetPoints(points)
                data.GetPointData().Initialize()
                data.GetCellData().Initialize()
                data.Modified()
                data, report, solid = self.analyze_print_mesh(data, part["name"])
                signatures = {}
                for source_id in part["source_ids"]:
                    source = self.objects[source_id]
                    digest = hashlib.sha256()
                    digest.update(vtk_to_numpy(source["mesh"].GetPoints().GetData()).tobytes())
                    digest.update(vtk_to_numpy(source["mesh"].GetPolys().GetData()).tobytes())
                    signatures[source_id] = digest.hexdigest()
                staged.append((data, part, signatures))
            self.push_undo()
            scope = self.output_scope.currentIndex() if hasattr(self, "output_scope") else 0
            selected_sources = set(source for data, part, signatures in staged for source in part["source_ids"])
            for key in list(self.objects):
                obj = self.objects[key]
                if obj["role"] == "print" and (scope != 1 or selected_sources.intersection(obj.get("source_ids", []))):
                    self.renderer.RemoveActor(obj["actor"])
                    del self.objects[key]
            for data, part, signatures in staged:
                self.add_object(data, "출력 · "+part["name"], "print", source_ids=part["source_ids"], source_signatures=signatures,
                                print_teeth=part["teeth"], print_bed_mm=bed.tolist(), print_rotation_deg=[self.print_rx.value(), self.print_ry.value(), self.print_rz.value()])
                for source_id in part["source_ids"]:
                    self.objects[source_id]["actor"].SetVisibility(False)
            for obj in self.objects.values():
                if obj["role"] != "print":
                    obj["actor"].SetVisibility(False)
            self.refresh_scene()
            if hasattr(self, "update_print_bed"):
                self.update_print_bed()
            self.camera_view("iso")
            self.dirty = True
            if hasattr(self, "print_info"):
                self.print_info.setText(f"출력판 배치 완료 · {len(staged)}개 제작물\n출력 공간 {bed[0]:g} × {bed[1]:g} × {bed[2]:g} mm\n배치 사본이 전체 내보내기에 우선 사용됩니다.\n서포트·레이어·노광 설정은 슬라이서에서 적용하세요.")
            self.statusBar().showMessage("출력 준비 완료 · 원본을 보존한 사본을 출력판에 배치했습니다.", 15000)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "출력판 배치 실패", str(error))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.busy = False

    def find_slicer(self):
        import re
        import winreg

        candidates = []
        names = ["chitubox", "lychee", "anycubicphotonworkshop", "photonworkshop", "orcaslicer", "prusa-slicer", "bambu studio", "bambu-studio", "ultimakercura", "crealityprint", "elegoo", "halot"]
        for root_key in [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]:
            for access in [winreg.KEY_READ | winreg.KEY_WOW64_64KEY, winreg.KEY_READ | winreg.KEY_WOW64_32KEY]:
                try:
                    base = winreg.OpenKey(root_key, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0, access)
                except OSError:
                    continue
                with base:
                    for index in range(winreg.QueryInfoKey(base)[0]):
                        try:
                            with winreg.OpenKey(base, winreg.EnumKey(base, index)) as entry:
                                display_name = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                                normalized = re.sub(r"[\s_\-]+", "", display_name.lower())
                                if not any(re.sub(r"[\s_\-]+", "", name) in normalized for name in names):
                                    continue
                                for value_name in ["DisplayIcon", "InstallLocation"]:
                                    try:
                                        value = os.path.expandvars(str(winreg.QueryValueEx(entry, value_name)[0])).strip()
                                    except OSError:
                                        continue
                                    icon_match = re.match(r'^"([^"]+\.exe)"', value, re.IGNORECASE)
                                    executable = icon_match.group(1) if icon_match else re.sub(r",\s*-?\d+$", "", value).strip('"')
                                    path = Path(executable)
                                    stem = re.sub(r"[\s_\-]+", "", path.stem.lower())
                                    if path.is_file() and path.suffix.lower() == ".exe" and any(re.sub(r"[\s_\-]+", "", name) in stem for name in names) and not any(word in stem for word in ["unins", "crash", "update", "maintenancetool"]):
                                        candidates.append((display_name, path))
                                    elif path.is_dir():
                                        for candidate in list(path.glob("*.exe"))+list(path.glob("bin/*.exe")):
                                            stem = re.sub(r"[\s_\-]+", "", candidate.stem.lower())
                                            if any(re.sub(r"[\s_\-]+", "", name) in stem for name in names) and not any(word in stem for word in ["unins", "crash", "update", "maintenancetool"]):
                                                candidates.append((display_name, candidate))
                        except OSError:
                            continue
        for variable in ["ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"]:
            root = Path(os.environ.get(variable, "C:/Program Files"))
            locations = [root]
            if variable == "LOCALAPPDATA":
                locations = [root/"Programs", root]
            for location in locations:
                if not location.exists():
                    continue
                try:
                    folders = [item for item in location.iterdir() if item.is_dir() and any(re.sub(r"[\s_\-]+", "", name) in re.sub(r"[\s_\-]+", "", item.name.lower()) for name in names)]
                except OSError:
                    continue
                for folder in folders:
                    for candidate in list(folder.glob("*.exe"))+list(folder.glob("bin/*.exe")):
                        stem = re.sub(r"[\s_\-]+", "", candidate.stem.lower())
                        if any(re.sub(r"[\s_\-]+", "", name) in stem for name in names) and not any(word in stem for word in ["unins", "crash", "update", "maintenancetool"]):
                            candidates.append((folder.name, candidate))
        unique = {}
        for name, path in candidates:
            unique[str(path.resolve()).lower()] = (name, path)
        candidates = sorted(unique.values(), key=lambda entry: entry[0].lower())
        if not candidates:
            self.statusBar().showMessage("설치된 슬라이서를 찾지 못했습니다. 슬라이서 EXE를 직접 지정해 주세요.", 10000)
            return False
        if len(candidates) == 1:
            path = candidates[0][1]
        else:
            labels = [f"{name} · {path}" for name, path in candidates]
            choice, accepted = QtWidgets.QInputDialog.getItem(self, "설치된 슬라이서 선택", "사용할 슬라이서", labels, 0, False)
            if not accepted:
                return False
            path = candidates[labels.index(choice)][1]
        self.slicer_path.setText(str(path))
        QtCore.QSettings("EXOCADE", "DentalDesign").setValue("slicer_path", str(path))
        self.statusBar().showMessage("슬라이서 연결 · "+str(path), 10000)
        return True

    def choose_slicer(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "사용할 슬라이서 실행 파일", self.slicer_path.text(), "Windows 프로그램 (*.exe)")
        if not path:
            return
        candidate = Path(path)
        if not candidate.is_file() or candidate.suffix.lower() != ".exe":
            QtWidgets.QMessageBox.warning(self, "슬라이서 경로", "실제 슬라이서 EXE 파일을 선택해 주세요.")
            return
        self.slicer_path.setText(str(candidate.resolve()))
        QtCore.QSettings("EXOCADE", "DentalDesign").setValue("slicer_path", str(candidate.resolve()))

    def open_slicer(self):
        import subprocess

        paths = [str(Path(path).resolve()) for path in getattr(self, "last_export_paths", []) if Path(path).is_file()]
        if not paths:
            QtWidgets.QMessageBox.information(self, "출력 파일 필요", "먼저 STL 또는 3MF를 저장해 주세요.")
            return False
        candidate = Path(os.path.expandvars(self.slicer_path.text().strip().strip('"')))
        if not candidate.is_file() or candidate.suffix.lower() != ".exe":
            if not self.find_slicer():
                return False
            candidate = Path(self.slicer_path.text())
        if getattr(sys, "frozen", False) and candidate.resolve() == Path(sys.executable).resolve():
            QtWidgets.QMessageBox.warning(self, "슬라이서 경로", "EXOCADE가 아닌 슬라이서 실행 파일을 선택해 주세요.")
            return False
        try:
            subprocess.Popen([str(candidate.resolve())]+paths, cwd=str(candidate.parent), shell=False)
            QtCore.QSettings("EXOCADE", "DentalDesign").setValue("slicer_path", str(candidate.resolve()))
            self.statusBar().showMessage("슬라이서로 모델 파일을 열었습니다. 제조사 출력 파일은 슬라이서에서 생성해 주세요.", 15000)
            return True
        except OSError as error:
            QtWidgets.QMessageBox.warning(self, "슬라이서 실행 실패", str(error))
            return False

    def build_print_model(self):
        if self.busy:
            return
        source_id = self.active_id
        source = self.objects.get(source_id)
        if source is None or source["role"] not in ["scan", "reference", "model"]:
            self.statusBar().showMessage("작업악 스캔 또는 모델을 선택하세요.", 6000)
            return
        trim_z = self.model_trim_plane.value()
        base_height = self.model_height.value()
        wall = self.model_wall.value()
        hollow = self.model_hollow.isChecked()
        drain_diameter = self.model_drain.value()
        source_bounds = source["mesh"].GetBounds()
        if not source_bounds[4]+0.01 < trim_z < source_bounds[5]-0.25:
            self.statusBar().showMessage("절단 Z 높이를 모델의 최저점과 최고점 사이로 지정하세요.", 8000)
            return
        if hollow and base_height < 2*wall+0.5:
            self.statusBar().showMessage("베이스 높이는 벽두께의 두 배보다 0.5 mm 이상 커야 합니다.", 8000)
            return
        self.busy = True
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        self.statusBar().showMessage("스캔 폐합 상태를 확인하고 인쇄용 베이스를 생성하고 있습니다…")
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        try:
            triangle = vtk.vtkTriangleFilter()
            triangle.SetInputData(source["mesh"])
            triangle.PassLinesOff()
            triangle.PassVertsOff()
            clean = vtk.vtkCleanPolyData()
            clean.SetInputConnection(triangle.GetOutputPort())
            clean.Update()
            data = vtk.vtkPolyData()
            data.DeepCopy(clean.GetOutput())
            points = np.asarray(vtk_to_numpy(data.GetPoints().GetData()), dtype=np.float64)
            if not len(points) or not np.isfinite(points).all():
                raise ValueError("스캔 좌표가 비어 있거나 유효하지 않습니다.")
            triangles = vtk_to_numpy(data.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
            directed = np.vstack([triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]])
            _, inverse, counts = np.unique(np.sort(directed, axis=1), axis=0, return_inverse=True, return_counts=True)
            if np.any(counts > 2):
                raise ValueError("스캔에 비다양체 모서리가 있습니다. 메시 정리 후 다시 실행하세요.")
            if np.any(counts == 1):
                plane = vtk.vtkPlane()
                plane.SetOrigin(0, 0, trim_z)
                plane.SetNormal(0, 0, 1)
                clip = vtk.vtkClipPolyData()
                clip.SetInputData(data)
                clip.SetClipFunction(plane)
                clip.InsideOutOff()
                clip.SetValue(0)
                triangle.SetInputConnection(clip.GetOutputPort())
                clean.SetInputConnection(triangle.GetOutputPort())
                clean.Update()
                data = vtk.vtkPolyData()
                data.DeepCopy(clean.GetOutput())
                if not data.GetNumberOfPolys():
                    raise ValueError("절단 높이 위에 남은 스캔 표면이 없습니다.")
                points = np.asarray(vtk_to_numpy(data.GetPoints().GetData()), dtype=np.float64)
                triangles = vtk_to_numpy(data.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
                directed = np.vstack([triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]])
                _, inverse, counts = np.unique(np.sort(directed, axis=1), axis=0, return_inverse=True, return_counts=True)
                if np.any(counts > 2):
                    raise ValueError("절단 후 비다양체 모서리가 남았습니다. 스캔을 먼저 정리하세요.")
                boundary = directed[counts[inverse] == 1]
                if not len(boundary):
                    raise ValueError("절단면 윤곽을 얻지 못했습니다. 다른 Z 높이를 지정하세요.")
                boundary_ids = np.unique(boundary)
                if np.max(np.abs(points[boundary_ids, 2]-trim_z)) > 0.03:
                    raise ValueError("절단면 위쪽에 열린 테두리나 구멍이 남았습니다. 절단 Z 높이를 올리거나 스캔 구멍을 먼저 정리하세요.")
                adjacency = {}
                for a, b in boundary:
                    adjacency.setdefault(int(a), []).append(int(b))
                    adjacency.setdefault(int(b), []).append(int(a))
                if any(len(neighbours) != 2 for neighbours in adjacency.values()):
                    raise ValueError("절단면의 테두리가 갈라져 있습니다. 스캔을 먼저 정리하세요.")
                start = int(boundary_ids[0])
                loop, previous, current = [], -1, start
                while current not in loop:
                    loop.append(current)
                    neighbours = adjacency[current]
                    following = neighbours[0] if neighbours[0] != previous else neighbours[1]
                    previous, current = current, following
                if current != start or len(loop) != len(boundary_ids):
                    raise ValueError("여러 개의 열린 테두리가 있습니다. 연결된 작업악 스캔을 사용하거나 절단 높이를 변경하세요.")
                polygon = points[loop, :2]
                area = 0.5*np.sum(polygon[:, 0]*np.roll(polygon[:, 1], -1)-polygon[:, 1]*np.roll(polygon[:, 0], -1))
                if abs(area) < 0.1:
                    raise ValueError("절단면의 면적이 너무 작습니다.")
                for i in range(len(polygon)):
                    indices = np.arange(i+2, len(polygon))
                    if i == 0:
                        indices = indices[indices < len(polygon)-1]
                    if not len(indices):
                        continue
                    a, b = polygon[i], polygon[(i+1) % len(polygon)]
                    c, d = polygon[indices], polygon[(indices+1) % len(polygon)]
                    ab, cd = b-a, d-c
                    first = ab[0]*(c[:, 1]-a[1])-ab[1]*(c[:, 0]-a[0])
                    second = ab[0]*(d[:, 1]-a[1])-ab[1]*(d[:, 0]-a[0])
                    third = cd[:, 0]*(a[1]-c[:, 1])-cd[:, 1]*(a[0]-c[:, 0])
                    fourth = cd[:, 0]*(b[1]-c[:, 1])-cd[:, 1]*(b[0]-c[:, 0])
                    if np.any((first*second < -1e-10) & (third*fourth < -1e-10)):
                        raise ValueError("절단면 윤곽이 서로 교차합니다. 다른 절단 높이를 사용하세요.")
                contours = vtk.vtkPolyData()
                contours.SetPoints(data.GetPoints())
                lines = vtk.vtkCellArray()
                for i, first in enumerate(loop):
                    lines.InsertNextCell(2)
                    lines.InsertCellPoint(first)
                    lines.InsertCellPoint(loop[(i+1) % len(loop)])
                contours.SetLines(lines)
                cap = vtk.vtkContourTriangulator()
                cap.SetInputData(contours)
                cap.Update()
                if not cap.GetOutput().GetNumberOfPolys():
                    raise ValueError("스캔 절단면을 닫지 못했습니다.")
                append = vtk.vtkAppendPolyData()
                append.AddInputData(data)
                append.AddInputData(cap.GetOutput())
                triangle.SetInputConnection(append.GetOutputPort())
                clean.SetInputConnection(triangle.GetOutputPort())
                clean.Update()
                data = vtk.vtkPolyData()
                data.DeepCopy(clean.GetOutput())
            normals = vtk.vtkPolyDataNormals()
            normals.SetInputData(data)
            normals.SplittingOff()
            normals.ConsistencyOn()
            normals.AutoOrientNormalsOn()
            normals.Update()
            data = normals.GetOutput()
            points = np.ascontiguousarray(vtk_to_numpy(data.GetPoints().GetData()), dtype=np.float64)
            triangles = np.ascontiguousarray(vtk_to_numpy(data.GetPolys().GetData()).reshape(-1, 4)[:, 1:], dtype=np.uint64)
            solid = manifold3d.Manifold(manifold3d.Mesh64(points, triangles))
            if solid.status() != manifold3d.Error.NoError or solid.is_empty():
                raise ValueError("스캔을 닫힌 입체로 만들 수 없습니다. 열린 경계와 중복 표면을 먼저 정리하세요.")
            components = solid.decompose()
            solid = manifold3d.Manifold.batch_boolean(components, manifold3d.OpType.Add)
            solid = solid.trim_by_plane((0, 0, 1), trim_z)
            if solid.status() != manifold3d.Error.NoError or solid.is_empty():
                raise ValueError("스캔의 수평 절단에 실패했습니다.")
            section = solid.slice(trim_z+0.01)
            if section.is_empty() or section.area() < 0.5:
                raise ValueError("베이스를 지지할 수평 단면이 없습니다. 절단 Z 높이를 변경하세요.")
            footprint = section.offset(0.8, manifold3d.JoinType.Round, circular_segments=48)
            base_z = trim_z-base_height
            base = footprint.extrude(base_height+0.02).translate((0, 0, base_z))
            solid = solid+base
            drain_count = 0
            if hollow:
                cavity_section = footprint.offset(-wall, manifold3d.JoinType.Round, circular_segments=48)
                if cavity_section.is_empty() or cavity_section.area() < 1:
                    raise ValueError("지정한 벽두께로 베이스를 비울 공간이 없습니다.")
                cavity_height = base_height-2*wall
                cavity_top = base_z+wall+cavity_height
                cavity = cavity_section.extrude(cavity_height).translate((0, 0, base_z+wall))
                solid = solid-cavity
                cavity_parts = cavity_section.decompose()
                if len(cavity_parts) > 8:
                    raise ValueError("분리된 내부 공간이 너무 많습니다. 모델 연결부 또는 벽두께를 확인하세요.")
                for part in cavity_parts:
                    polygons = part.to_polygons()
                    coordinates = np.vstack(polygons)
                    lower, upper = coordinates.min(axis=0), coordinates.max(axis=0)
                    radius = drain_diameter/2
                    candidates = []
                    for x in np.linspace(lower[0]+radius, upper[0]-radius, 17):
                        for y in np.linspace(lower[1]+radius, upper[1]-radius, 17):
                            circle = manifold3d.CrossSection.circle(radius+0.15, 40).translate((float(x), float(y)))
                            if (circle-part).area() < 1e-6:
                                candidates.append([float(x), float(y)])
                    if len(candidates) < 2:
                        raise ValueError("내부 공간에 배수구 두 개를 배치할 수 없습니다. 배수구 지름이나 벽두께를 줄이세요.")
                    candidates = np.asarray(candidates)
                    first = int(np.argmax(np.linalg.norm(candidates-candidates.mean(axis=0), axis=1)))
                    second = int(np.argmax(np.linalg.norm(candidates-candidates[first], axis=1)))
                    if np.linalg.norm(candidates[first]-candidates[second]) < drain_diameter+0.5:
                        raise ValueError("배수구 사이 간격을 확보하지 못했습니다. 배수구 지름을 줄이세요.")
                    for x, y in candidates[[first, second]]:
                        drain = manifold3d.Manifold.cylinder(cavity_top-base_z+0.2, radius, circular_segments=48).translate((float(x), float(y), base_z-0.1))
                        solid = solid-drain
                        drain_count += 1
            if solid.status() != manifold3d.Error.NoError or solid.is_empty() or solid.volume() <= 0:
                raise ValueError("인쇄용 모델 생성에 실패했습니다.")
            coordinate_scale = max(1., float(np.max(np.abs(points)))+base_height)
            cleanup_tolerance = max(1e-5, float(np.spacing(np.float32(coordinate_scale)))*2)
            solid = solid.as_original()
            while cleanup_tolerance <= 0.001:
                simplified = solid.simplify(cleanup_tolerance)
                if simplified.status() != manifold3d.Error.NoError or simplified.is_empty():
                    raise ValueError("출력 정밀도에 맞춘 모델 정리에 실패했습니다.")
                result = simplified.to_mesh64()
                quantized = np.asarray(result.vert_properties[:, :3], dtype=np.float32).astype(np.float64)
                result_points, remap = np.unique(quantized, axis=0, return_inverse=True)
                result_triangles = remap[np.asarray(result.tri_verts, dtype=np.int64)]
                triangle_points = result_points[result_triangles]
                areas = np.linalg.norm(np.cross(triangle_points[:, 1]-triangle_points[:, 0], triangle_points[:, 2]-triangle_points[:, 0]), axis=1)
                directed = np.vstack([result_triangles[:, [0, 1]], result_triangles[:, [1, 2]], result_triangles[:, [2, 0]]])
                _, counts = np.unique(np.sort(directed, axis=1), axis=0, return_counts=True)
                if np.isfinite(result_points).all() and np.all(areas > 1e-12) and np.all(counts == 2):
                    quantized_solid = manifold3d.Manifold(manifold3d.Mesh64(np.ascontiguousarray(result_points), np.ascontiguousarray(result_triangles, dtype=np.uint64)))
                    if quantized_solid.status() == manifold3d.Error.NoError and not quantized_solid.is_empty() and quantized_solid.volume() > 0:
                        break
                cleanup_tolerance *= 2
            else:
                raise ValueError("STL 정밀도에서 폐합과 삼각형 품질을 유지할 수 없습니다. 모델을 원점 가까이 이동하거나 스캔을 정리하세요.")
            mesh = vtk.vtkPolyData()
            vtk_points = vtk.vtkPoints()
            vtk_points.SetData(numpy_to_vtk(result_points, deep=True))
            mesh.SetPoints(vtk_points)
            cells = vtk.vtkCellArray()
            cells.SetCells(len(result_triangles), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(result_triangles), 3), result_triangles]).astype(np.int64).ravel(), deep=True))
            mesh.SetPolys(cells)
            clean.SetInputData(mesh)
            clean.Update()
            mesh = clean.GetOutput()
            edges = vtk.vtkFeatureEdges()
            edges.SetInputData(mesh)
            edges.BoundaryEdgesOn()
            edges.NonManifoldEdgesOn()
            edges.FeatureEdgesOff()
            edges.ManifoldEdgesOff()
            edges.Update()
            if edges.GetOutput().GetNumberOfCells():
                raise ValueError("완성된 모델에 열린 경계가 있어 생성을 취소했습니다.")
            self.push_undo()
            source["actor"].SetVisibility(False)
            self.add_object(mesh, source["name"]+" · "+("중공 베이스" if hollow else "솔리드 베이스"), "model",
                            source_id=source_id, print_model=True, base_height=base_height, base_trim_z=trim_z,
                            hollow_base=hollow, wall=wall if hollow else None, drain_diameter=drain_diameter if hollow else None,
                            drain_count=drain_count, stl_precision=True, cleanup_tolerance_mm=cleanup_tolerance, color=(0.75, 0.80, 0.86))
            self.camera_view("fit")
            self.statusBar().showMessage(f"인쇄용 모델 생성 완료 · 평면 베이스 {base_height:.1f} mm · 배수구 {drain_count}개 · 닫힌 메시", 15000)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "인쇄용 모델 생성 실패", str(error))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.busy = False

    def extract_die(self):
        if self.busy:
            return
        state = self.teeth.get(str(self.current_tooth), {})
        margin = np.asarray(state.get("margin", []), dtype=np.float64)
        if not state.get("closed") or len(margin) < 6:
            self.statusBar().showMessage("분리할 치아의 마진선을 지정하고 닫아주세요.", 8000)
            return
        source_id = self.active_id
        source = self.objects.get(source_id)
        if source is None or source["role"] not in ["model", "scan", "reference"]:
            source_id = state.get("scan_id")
            source = self.objects.get(source_id)
        if source is None:
            self.statusBar().showMessage("마진이 속한 작업악 스캔 또는 인쇄용 모델이 필요합니다.", 7000)
            return
        self.busy = True
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        self.statusBar().showMessage("마진 주변을 절단해 분리 다이를 생성하고 있습니다…")
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        try:
            triangle = vtk.vtkTriangleFilter()
            triangle.SetInputData(source["mesh"])
            triangle.PassLinesOff()
            triangle.PassVertsOff()
            clean = vtk.vtkCleanPolyData()
            clean.SetInputConnection(triangle.GetOutputPort())
            clean.Update()
            normals = vtk.vtkPolyDataNormals()
            normals.SetInputConnection(clean.GetOutputPort())
            normals.SplittingOff()
            normals.ConsistencyOn()
            normals.AutoOrientNormalsOn()
            normals.Update()
            data = normals.GetOutput()
            points = np.ascontiguousarray(vtk_to_numpy(data.GetPoints().GetData()), dtype=np.float64)
            triangles = np.ascontiguousarray(vtk_to_numpy(data.GetPolys().GetData()).reshape(-1, 4)[:, 1:], dtype=np.uint64)
            solid = manifold3d.Manifold(manifold3d.Mesh64(points, triangles))
            if solid.status() != manifold3d.Error.NoError or solid.is_empty():
                raise ValueError("열린 스캔에서는 다이를 분리할 수 없습니다. 먼저 인쇄용 모델을 생성한 후 그 모델을 선택하세요.")
            solid = manifold3d.Manifold.batch_boolean(solid.decompose(), manifold3d.OpType.Add)
            axis = np.asarray(state.get("axis", [0, 0, 1]), dtype=float)
            if not np.isfinite(axis).all() or np.linalg.norm(axis) < 1e-8:
                raise ValueError("치아 삽입축이 유효하지 않습니다.")
            axis /= np.linalg.norm(axis)
            basis_x = np.array([1., 0, 0]) if abs(axis[0]) < 0.85 else np.array([0., 1, 0])
            basis_x -= axis*np.dot(basis_x, axis)
            basis_x /= np.linalg.norm(basis_x)
            basis_y = np.cross(axis, basis_x)
            basis = np.column_stack([basis_x, basis_y, axis])
            origin = margin.mean(axis=0)
            local_margin = (margin-origin) @ basis
            local_points = (points-origin) @ basis
            polygon = local_margin[:, :2]
            if not np.isfinite(polygon).all() or np.min(np.linalg.norm(np.roll(polygon, -1, axis=0)-polygon, axis=1)) < 0.001:
                raise ValueError("마진 좌표가 유효하지 않거나 같은 위치의 점이 중복되어 있습니다.")
            area = 0.5*np.sum(polygon[:, 0]*np.roll(polygon[:, 1], -1)-polygon[:, 1]*np.roll(polygon[:, 0], -1))
            if abs(area) < 0.1:
                raise ValueError("삽입축에서 본 마진 면적이 너무 작습니다.")
            for i in range(len(polygon)):
                indices = np.arange(i+2, len(polygon))
                if i == 0:
                    indices = indices[indices < len(polygon)-1]
                if not len(indices):
                    continue
                a, b = polygon[i], polygon[(i+1) % len(polygon)]
                c, d = polygon[indices], polygon[(indices+1) % len(polygon)]
                ab, cd = b-a, d-c
                first = ab[0]*(c[:, 1]-a[1])-ab[1]*(c[:, 0]-a[0])
                second = ab[0]*(d[:, 1]-a[1])-ab[1]*(d[:, 0]-a[0])
                third = cd[:, 0]*(a[1]-c[:, 1])-cd[:, 1]*(a[0]-c[:, 0])
                fourth = cd[:, 0]*(b[1]-c[:, 1])-cd[:, 1]*(b[0]-c[:, 0])
                if np.any((first*second < -1e-10) & (third*fourth < -1e-10)):
                    raise ValueError("삽입축에서 본 마진 윤곽이 서로 교차합니다. 마진 또는 삽입축을 수정하세요.")
            if area < 0:
                polygon = polygon[::-1]
            control = getattr(self, "die_offset", None)
            offset = control.value() if control is not None else 0.5
            outline = manifold3d.CrossSection([np.ascontiguousarray(polygon)], manifold3d.FillRule.NonZero).offset(offset, manifold3d.JoinType.Round, circular_segments=64)
            if outline.is_empty() or outline.area() < 0.5:
                raise ValueError("다이의 외곽을 생성하지 못했습니다.")
            floor = float(local_margin[:, 2].min()-self.model_height.value())
            top = float(local_points[:, 2].max()+1)
            matrix = np.column_stack([basis, origin])
            cutter = outline.extrude(top-floor).translate((0, 0, floor)).transform(matrix)
            result = manifold3d.Manifold.batch_boolean([solid, cutter], manifold3d.OpType.Intersect)
            pedestal_height = float(local_margin[:, 2].min()-0.3-floor)
            if pedestal_height > 0:
                pedestal = outline.extrude(pedestal_height).translate((0, 0, floor)).transform(matrix)
                result = result+pedestal
            if result.status() != manifold3d.Error.NoError or result.is_empty() or result.volume() <= 0:
                raise ValueError("마진 위치에서 분리할 입체를 찾지 못했습니다.")
            if len(result.decompose()) != 1:
                raise ValueError("분리 다이에 떨어진 조각이 남았습니다. 마진과 다이 여유 폭을 확인하세요.")
            coordinate_scale = max(1., float(np.max(np.abs(points)))+self.model_height.value()+offset)
            cleanup_tolerance = max(1e-5, float(np.spacing(np.float32(coordinate_scale)))*2)
            result = result.as_original()
            while cleanup_tolerance <= 0.001:
                simplified = result.simplify(cleanup_tolerance)
                if simplified.status() != manifold3d.Error.NoError or simplified.is_empty():
                    raise ValueError("출력 정밀도에 맞춘 다이 정리에 실패했습니다.")
                output = simplified.to_mesh64()
                quantized = np.asarray(output.vert_properties[:, :3], dtype=np.float32).astype(np.float64)
                vertices, remap = np.unique(quantized, axis=0, return_inverse=True)
                faces = remap[np.asarray(output.tri_verts, dtype=np.int64)]
                triangle_points = vertices[faces]
                areas = np.linalg.norm(np.cross(triangle_points[:, 1]-triangle_points[:, 0], triangle_points[:, 2]-triangle_points[:, 0]), axis=1)
                directed = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
                _, counts = np.unique(np.sort(directed, axis=1), axis=0, return_counts=True)
                if np.isfinite(vertices).all() and np.all(areas > 1e-12) and np.all(counts == 2):
                    quantized_solid = manifold3d.Manifold(manifold3d.Mesh64(np.ascontiguousarray(vertices), np.ascontiguousarray(faces, dtype=np.uint64)))
                    if quantized_solid.status() == manifold3d.Error.NoError and not quantized_solid.is_empty() and quantized_solid.volume() > 0:
                        break
                cleanup_tolerance *= 2
            else:
                raise ValueError("STL 정밀도에서 다이의 폐합과 삼각형 품질을 유지할 수 없습니다. 모델을 원점 가까이 이동하거나 스캔을 정리하세요.")
            mesh = vtk.vtkPolyData()
            vtk_points = vtk.vtkPoints()
            vtk_points.SetData(numpy_to_vtk(vertices, deep=True))
            mesh.SetPoints(vtk_points)
            cells = vtk.vtkCellArray()
            cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).astype(np.int64).ravel(), deep=True))
            mesh.SetPolys(cells)
            clean.SetInputData(mesh)
            clean.Update()
            mesh = clean.GetOutput()
            edges = vtk.vtkFeatureEdges()
            edges.SetInputData(mesh)
            edges.BoundaryEdgesOn()
            edges.NonManifoldEdgesOn()
            edges.FeatureEdgesOff()
            edges.ManifoldEdgesOff()
            edges.Update()
            if edges.GetOutput().GetNumberOfCells():
                raise ValueError("분리 다이에 열린 경계가 있어 생성을 취소했습니다.")
            self.push_undo()
            source["actor"].SetVisibility(False)
            self.add_object(mesh, f"{self.current_tooth} · 마진 분리 다이", "die", source_id=source_id,
                            source_tooth=self.current_tooth, axis=axis.tolist(), die_offset=offset,
                            print_model=True, keyed_socket=False, stl_precision=True, cleanup_tolerance_mm=cleanup_tolerance, color=(0.56, 0.78, 0.85))
            self.camera_view("fit")
            self.statusBar().showMessage(f"{self.current_tooth}번 분리 다이 생성 완료 · 닫힌 단일 입체 · 핀/소켓 구조 없음", 15000)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "다이 분리 실패", str(error))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.busy = False

    def show_undercuts(self):
        obj = self.objects.get(self.active_id)
        if obj is None:
            self.statusBar().showMessage("역경사를 확인할 작업악 또는 모델을 선택하세요.", 6000)
            return
        state = self.teeth.get(str(self.current_tooth), {})
        axis = np.asarray(state.get("axis", [0, 0, 1]), dtype=float)
        if not np.isfinite(axis).all() or np.linalg.norm(axis) < 1e-8:
            self.statusBar().showMessage("먼저 유효한 삽입축을 지정하세요.", 6000)
            return
        axis /= np.linalg.norm(axis)
        mesh = obj["mesh"]
        normals = vtk.vtkPolyDataNormals()
        normals.SetInputData(mesh)
        normals.SplittingOff()
        normals.ConsistencyOn()
        normals.ComputePointNormalsOn()
        normals.ComputeCellNormalsOn()
        normals.Update()
        normal_data = normals.GetOutput().GetPointData().GetNormals()
        if normal_data is None or normal_data.GetNumberOfTuples() != mesh.GetNumberOfPoints():
            self.statusBar().showMessage("이 메시에서는 법선을 계산할 수 없습니다.", 6000)
            return
        point_normals = vtk_to_numpy(normal_data).astype(float)
        values = np.degrees(np.arcsin(np.clip(-(point_normals @ axis), -1, 1)))
        values = np.maximum(values, 0)
        triangle_filter = vtk.vtkTriangleFilter()
        triangle_filter.SetInputData(normals.GetOutput())
        triangle_filter.Update()
        triangles_mesh = triangle_filter.GetOutput()
        vertices = vtk_to_numpy(triangles_mesh.GetPoints().GetData())
        faces = vtk_to_numpy(triangles_mesh.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
        cross = np.cross(vertices[faces[:, 1]]-vertices[faces[:, 0]], vertices[faces[:, 2]]-vertices[faces[:, 0]])
        lengths = np.linalg.norm(cross, axis=1)
        face_normals = cross/np.maximum(lengths[:, None], 1e-12)
        face_angles = np.degrees(np.arcsin(np.clip(-(face_normals @ axis), -1, 1)))
        threshold = self.undercut_angle.value()
        total_area = lengths.sum()/2
        undercut_area = lengths[face_angles > threshold].sum()/2
        if self.scalar_bar:
            self.renderer.RemoveViewProp(self.scalar_bar)
            self.scalar_bar = None
        scalar = numpy_to_vtk(values, deep=True)
        scalar.SetName("NormalUndercut_deg")
        mesh.GetPointData().SetScalars(scalar)
        lut = vtk.vtkLookupTable()
        lut.SetNumberOfTableValues(256)
        maximum = max(30., min(90., float(np.percentile(values, 98))))
        lut.SetRange(0, maximum)
        for index in range(256):
            angle = index/255*maximum
            if angle <= threshold:
                color = (0.36, 0.62, 0.70)
            else:
                fraction = min(1., (angle-threshold)/max(maximum-threshold, .01))
                color = (0.98, 0.78*(1-fraction)+0.18*fraction, 0.20*(1-fraction)+0.13*fraction)
            lut.SetTableValue(index, *color, 1.)
        lut.Build()
        mapper = obj["actor"].GetMapper()
        mapper.SetLookupTable(lut)
        mapper.SetScalarRange(0, maximum)
        mapper.ScalarVisibilityOn()
        self.scalar_bar = vtk.vtkScalarBarActor()
        self.scalar_bar.SetLookupTable(lut)
        self.scalar_bar.SetTitle("Normal undercut / deg")
        self.scalar_bar.SetNumberOfLabels(5)
        self.scalar_bar.SetWidth(.12)
        self.scalar_bar.SetHeight(.38)
        self.scalar_bar.SetPosition(.84, .12)
        self.scalar_bar.GetTitleTextProperty().SetFontSize(11)
        self.scalar_bar.GetLabelTextProperty().SetFontSize(11)
        self.renderer.AddViewProp(self.scalar_bar)
        self.color_mode = "undercut_normal"
        message = (f"법선 기준 역경사 {undercut_area:.2f} mm² · {undercut_area/max(total_area, 1e-9)*100:.1f}% "
                   f"· 기준 {threshold:.1f}° · 시야 가림은 검사하지 않습니다.")
        self.inspection_info.setText(message)
        self.statusBar().showMessage(message, 20000)
        self.vtk_widget.GetRenderWindow().Render()

    def build_shell(self):
        if self.busy:
            return
        obj = self.objects.get(self.active_id)
        if obj is None or obj["role"] not in ["scan", "model", "reference", "die"]:
            self.statusBar().showMessage("기본 쉘을 만들 닫힌 작업악 또는 모델을 선택하세요.", 7000)
            return
        thickness = float(self.shell_thickness.value())
        gap = float(self.shell_gap.value())
        trim_z = float(self.shell_trim.value())
        kind = self.shell_kind.currentIndex()
        if thickness <= 0 or gap < 0:
            self.statusBar().showMessage("두께는 0보다 크게, 간극은 0 이상으로 지정하세요.", 6000)
            return
        bounds = np.asarray(obj["mesh"].GetBounds()).reshape(3, 2)
        if trim_z <= bounds[2, 0] or trim_z >= bounds[2, 1]-thickness*.25:
            self.statusBar().showMessage("절단 높이를 모델의 바닥과 교합면 사이에 지정하세요.", 8000)
            return
        self.busy = True
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        self.statusBar().showMessage("닫힌 모델에서 간극과 두께를 가진 기본 쉘을 생성하고 있습니다…")
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        try:
            triangle_filter = vtk.vtkTriangleFilter()
            triangle_filter.SetInputData(obj["mesh"])
            clean = vtk.vtkCleanPolyData()
            clean.SetInputConnection(triangle_filter.GetOutputPort())
            clean.Update()
            source = clean.GetOutput()
            edges = vtk.vtkFeatureEdges()
            edges.SetInputData(source)
            edges.BoundaryEdgesOn()
            edges.NonManifoldEdgesOn()
            edges.FeatureEdgesOff()
            edges.ManifoldEdgesOff()
            edges.Update()
            if edges.GetOutput().GetNumberOfCells():
                raise ValueError("열린 스캔은 기본 쉘로 변환할 수 없습니다. 먼저 인쇄용 닫힌 모델을 생성하세요.")
            vertices = np.ascontiguousarray(vtk_to_numpy(source.GetPoints().GetData()), dtype=np.float64)
            triangles = np.ascontiguousarray(vtk_to_numpy(source.GetPolys().GetData()).reshape(-1, 4)[:, 1:], dtype=np.uint64)
            solid = manifold3d.Manifold(manifold3d.Mesh64(vertices, triangles))
            if solid.status() != manifold3d.Error.NoError or solid.is_empty():
                raise ValueError("모델이 닫힌 고체로 인식되지 않습니다. 메시를 정리한 후 다시 시도하세요.")
            parts = solid.decompose()
            if solid.volume() <= 0:
                raise ValueError("모델의 면 방향 또는 체적이 잘못되었습니다. 법선을 정리한 후 다시 시도하세요.")
            if len(parts) > 1 and all(part.volume() > 0 for part in parts):
                solid = manifold3d.Manifold.batch_boolean(parts, manifold3d.OpType.Add)
            if solid.status() != manifold3d.Error.NoError or solid.is_empty():
                raise ValueError("서로 겹친 모델 표면을 결합하지 못했습니다.")
            source_data = solid.to_mesh64()
            source = vtk.vtkPolyData()
            source_points = vtk.vtkPoints()
            source_points.SetData(numpy_to_vtk(np.asarray(source_data.vert_properties[:, :3]).copy(), deep=True))
            source.SetPoints(source_points)
            source_faces = np.asarray(source_data.tri_verts, dtype=np.int64)
            source_cells = vtk.vtkCellArray()
            source_cells.SetCells(len(source_faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(source_faces), 3), source_faces]).astype(np.int64).ravel(), deep=True))
            source.SetPolys(source_cells)
            bounds = np.asarray(source.GetBounds()).reshape(3, 2)
            spacing = max(.15, min(.25, thickness/8))
            padding = gap+thickness+spacing*4
            origin = bounds[:, 0]-padding
            upper = bounds[:, 1]+padding
            span = upper-origin
            spacing = max(spacing, float((np.prod(span)/5_000_000)**(1/3)))
            dimensions = np.ceil(span/spacing).astype(int)+1
            if np.prod(dimensions) > 6_000_000 or dimensions.max() > 600:
                raise ValueError("쉘 생성 범위가 너무 큽니다. 모델의 단위를 확인하거나 필요한 부분만 남겨주세요.")
            shape = (int(dimensions[2]), int(dimensions[1]), int(dimensions[0]))
            image = vtk.vtkImageData()
            image.SetDimensions(*[int(value) for value in dimensions])
            image.SetSpacing(spacing, spacing, spacing)
            image.SetOrigin(*origin)
            initial = np.ones(int(np.prod(dimensions)), dtype=np.uint8)
            image.GetPointData().SetScalars(numpy_to_vtk(initial, deep=True))
            stencil = vtk.vtkPolyDataToImageStencil()
            stencil.SetInputData(source)
            stencil.SetOutputOrigin(*origin)
            stencil.SetOutputSpacing(spacing, spacing, spacing)
            stencil.SetOutputWholeExtent(image.GetExtent())
            stencil_filter = vtk.vtkImageStencil()
            stencil_filter.SetInputData(image)
            stencil_filter.SetStencilConnection(stencil.GetOutputPort())
            stencil_filter.SetBackgroundValue(0)
            stencil_filter.Update()
            occupancy = vtk_to_numpy(stencil_filter.GetOutput().GetPointData().GetScalars()).copy().astype(bool)
            if not occupancy.any() or occupancy.all():
                raise ValueError("모델 내부 영역을 찾을 수 없습니다. 법선과 폐합 상태를 확인하세요.")
            distance_input = vtk.vtkImageData()
            distance_input.DeepCopy(image)
            distance_input.SetSpacing(1., 1., 1.)
            distance_input.GetPointData().SetScalars(numpy_to_vtk((~occupancy).astype(np.uint8), deep=True))
            outside_distance = vtk.vtkImageEuclideanDistance()
            outside_distance.SetInputData(distance_input)
            outside_distance.InitializeOn()
            outside_distance.ConsiderAnisotropyOff()
            outside_distance.SetAlgorithmToSaitoCached()
            outside_distance.SetMaximumDistance(((padding*2+spacing*4)/spacing)**2)
            outside_distance.Update()
            outside = np.sqrt(vtk_to_numpy(outside_distance.GetOutput().GetPointData().GetScalars())).copy()*spacing
            distance_input.GetPointData().SetScalars(numpy_to_vtk(occupancy.astype(np.uint8), deep=True))
            inside_distance = vtk.vtkImageEuclideanDistance()
            inside_distance.SetInputData(distance_input)
            inside_distance.InitializeOn()
            inside_distance.ConsiderAnisotropyOff()
            inside_distance.SetAlgorithmToSaitoCached()
            inside_distance.SetMaximumDistance(((padding*2+spacing*4)/spacing)**2)
            inside_distance.Update()
            signed = outside-np.sqrt(vtk_to_numpy(inside_distance.GetOutput().GetPointData().GetScalars()))*spacing
            field = np.maximum(gap-signed, signed-(gap+thickness)).reshape(shape)
            world_z = origin[2]+np.arange(shape[0])*spacing
            np.maximum(field, (trim_z-world_z)[:, None, None], out=field)
            field_image = vtk.vtkImageData()
            field_image.DeepCopy(image)
            field_image.GetPointData().SetScalars(numpy_to_vtk(np.ascontiguousarray(field.ravel(), dtype=np.float32), deep=True))
            contour = vtk.vtkFlyingEdges3D()
            contour.SetInputData(field_image)
            contour.SetValue(0, 0)
            contour.ComputeNormalsOff()
            contour.Update()
            shell = vtk.vtkPolyData()
            shell.DeepCopy(contour.GetOutput())
            if shell.GetNumberOfPolys() == 0:
                raise ValueError("이 절단 높이에서는 기본 쉘을 생성할 수 없습니다.")
            shell_points = np.ascontiguousarray(vtk_to_numpy(shell.GetPoints().GetData()), dtype=np.float64)
            shell_faces = np.ascontiguousarray(vtk_to_numpy(shell.GetPolys().GetData()).reshape(-1, 4)[:, 1:], dtype=np.uint64)
            shell_triangles = shell_points[shell_faces]
            if np.einsum("ij,ij->i", shell_triangles[:, 0], np.cross(shell_triangles[:, 1], shell_triangles[:, 2])).sum() < 0:
                shell_faces = np.ascontiguousarray(shell_faces[:, [0, 2, 1]], dtype=np.uint64)
            result = manifold3d.Manifold(manifold3d.Mesh64(shell_points, shell_faces))
            if result.status() != manifold3d.Error.NoError or result.is_empty():
                raise ValueError("기본 쉘에 열린 경계가 발생했습니다. 절단 높이나 두께를 바꾸어 주세요.")
            if kind == 1 and self.shell_handle.isChecked():
                usable = shell_points[shell_points[:, 2] >= trim_z+1.5]
                if len(usable) == 0:
                    raise ValueError("손잡이를 연결할 높이가 부족합니다. 절단 높이를 낮추거나 손잡이를 해제하세요.")
                front = usable[np.argmax(usable[:, 1])]
                embed = min(.7, thickness*.45)
                handle = manifold3d.Manifold.cube([6., 10., 2.8], center=True).translate([float(front[0]), float(front[1]+5-embed), float(front[2])])
                result = result+handle
                field_image.GetPointData().SetScalars(numpy_to_vtk(np.ascontiguousarray(signed-gap, dtype=np.float32), deep=True))
                contour.Update()
                cavity_mesh = contour.GetOutput()
                cavity_points = np.ascontiguousarray(vtk_to_numpy(cavity_mesh.GetPoints().GetData()), dtype=np.float64)
                cavity_faces = np.ascontiguousarray(vtk_to_numpy(cavity_mesh.GetPolys().GetData()).reshape(-1, 4)[:, 1:], dtype=np.uint64)
                cavity_triangles = cavity_points[cavity_faces]
                if np.einsum("ij,ij->i", cavity_triangles[:, 0], np.cross(cavity_triangles[:, 1], cavity_triangles[:, 2])).sum() < 0:
                    cavity_faces = np.ascontiguousarray(cavity_faces[:, [0, 2, 1]], dtype=np.uint64)
                cavity = manifold3d.Manifold(manifold3d.Mesh64(cavity_points, cavity_faces))
                if cavity.status() != manifold3d.Error.NoError:
                    raise ValueError("손잡이와 내면 공간을 분리하지 못했습니다.")
                result = result-cavity
            if result.status() != manifold3d.Error.NoError or result.is_empty():
                raise ValueError("기본 쉘 결합에 실패했습니다.")
            original_result = result.as_original()
            for tolerance in [1e-5, 3e-5, 1e-4, 3e-4, .001]:
                simplified = original_result.simplify(tolerance)
                if simplified.status() != manifold3d.Error.NoError or simplified.is_empty():
                    continue
                final_mesh = simplified.to_mesh64()
                quantized = np.asarray(final_mesh.vert_properties[:, :3], dtype=np.float32).astype(np.float64)
                final_points, remap = np.unique(quantized, axis=0, return_inverse=True)
                final_faces = remap[np.asarray(final_mesh.tri_verts, dtype=np.int64)]
                coordinates = final_points[final_faces]
                cross = np.cross(coordinates[:, 1]-coordinates[:, 0], coordinates[:, 2]-coordinates[:, 0])
                if not len(cross) or np.any(np.linalg.norm(cross, axis=1) <= 1e-12):
                    continue
                edge_pairs = np.vstack([final_faces[:, [0, 1]], final_faces[:, [1, 2]], final_faces[:, [2, 0]]])
                _, edge_counts = np.unique(np.sort(edge_pairs, axis=1), axis=0, return_counts=True)
                if np.any(edge_counts != 2):
                    continue
                result = manifold3d.Manifold(manifold3d.Mesh64(np.ascontiguousarray(final_points, dtype=np.float64), np.ascontiguousarray(final_faces, dtype=np.uint64)))
                if result.status() == manifold3d.Error.NoError and not result.is_empty() and result.volume() > 0 and len(result.decompose()) == 1:
                    break
            else:
                raise ValueError("STL 정밀도에서 닫힌 단일 쉘을 확보하지 못했습니다. 절단 높이나 두께를 변경하세요.")
            points = vtk.vtkPoints()
            points.SetData(numpy_to_vtk(final_points, deep=True))
            cells = vtk.vtkCellArray()
            faces = final_faces
            cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).astype(np.int64).ravel(), deep=True))
            output = vtk.vtkPolyData()
            output.SetPoints(points)
            output.SetPolys(cells)
            edges.SetInputData(output)
            edges.Update()
            if edges.GetOutput().GetNumberOfCells():
                raise ValueError("기본 쉘의 폐합 검사에 실패했습니다.")
            self.push_undo()
            name = "스플린트 기본 쉘" if kind == 0 else "개인 트레이 기본 쉘"
            self.add_object(output, name, "appliance", source_id=self.active_id, appliance_type="splint_shell" if kind == 0 else "tray_shell",
                            nominal_wall=thickness, gap=gap, trim_z=trim_z, voxel_size=spacing, handle=bool(kind == 1 and self.shell_handle.isChecked()),
                            color=(.46, .78, .86) if kind == 0 else (.63, .77, .53))
            message = f"{name} 생성 완료 · 계산 간격 {spacing:.3f} mm · 언더컷·내면 적합·두께·교합을 별도로 확인하세요."
            self.statusBar().showMessage(message, 20000)
            self.focus_selection()
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "기본 쉘 생성 실패", str(error))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.busy = False

    def show_printer_dialog(self):
        if self.printer_dialog is None:
            dialog = QtWidgets.QDialog(self)
            self.printer_dialog = dialog
            dialog.setWindowTitle("프린터 검색 · 선택")
            dialog.resize(1040, 740)
            layout = QtWidgets.QVBoxLayout(dialog)
            title = QtWidgets.QLabel("인식된 프린터를 선택하세요")
            title.setObjectName("panelTitle")
            layout.addWidget(title)
            note = QtWidgets.QLabel("USB·Windows 장치와 같은 네트워크의 지원 서비스를 검색합니다. 검색되지 않으면 IP 주소로 등록하세요.")
            note.setWordWrap(True)
            layout.addWidget(note)
            row = QtWidgets.QHBoxLayout()
            self.printer_search_button = QtWidgets.QPushButton("프린터 검색")
            self.printer_search_button.clicked.connect(self.discover_printers)
            row.addWidget(self.printer_search_button)
            self.printer_network = QtWidgets.QCheckBox("네트워크 검색")
            self.printer_network.setChecked(True)
            row.addWidget(self.printer_network)
            self.printer_show_other = QtWidgets.QCheckBox("기타 장치 표시")
            self.printer_show_other.toggled.connect(self.refresh_printer_table)
            row.addWidget(self.printer_show_other)
            row.addStretch()
            self.printer_cancel_button = QtWidgets.QPushButton("작업 취소")
            self.printer_cancel_button.clicked.connect(self.cancel_printer_task)
            row.addWidget(self.printer_cancel_button)
            layout.addLayout(row)
            self.printer_table = QtWidgets.QTableWidget(0, 5)
            self.printer_table.setHorizontalHeaderLabels(["프린터", "연결", "주소 / 포트", "상태", "프로그램 지원"])
            self.printer_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.printer_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
            self.printer_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
            self.printer_table.verticalHeader().hide()
            self.printer_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            self.printer_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
            self.printer_table.setMinimumHeight(180)
            self.printer_table.currentCellChanged.connect(self.printer_row_changed)
            layout.addWidget(self.printer_table, 1)
            self.printer_detail = QtWidgets.QLabel()
            self.printer_detail.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            self.printer_detail.setWordWrap(True)
            self.printer_detail.setMinimumHeight(44)
            layout.addWidget(self.printer_detail)
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel("API 키"))
            self.printer_key = QtWidgets.QLineEdit()
            self.printer_key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            self.printer_key.setMaxLength(4096)
            self.printer_key.setPlaceholderText("필요한 경우 입력 · 이 실행 중에만 보관")
            self.printer_key.textEdited.connect(self.printer_key_changed)
            row.addWidget(self.printer_key, 1)
            self.printer_check_button = QtWidgets.QPushButton("연결 상태 확인")
            self.printer_check_button.clicked.connect(self.check_printer)
            row.addWidget(self.printer_check_button)
            self.printer_remove_button = QtWidgets.QPushButton("등록 삭제")
            self.printer_remove_button.clicked.connect(self.remove_printer)
            row.addWidget(self.printer_remove_button)
            layout.addLayout(row)
            group = QtWidgets.QGroupBox("IP 주소로 직접 등록")
            manual = QtWidgets.QHBoxLayout(group)
            self.printer_name_input = QtWidgets.QLineEdit()
            self.printer_name_input.setPlaceholderText("프린터 이름")
            self.printer_name_input.setMaxLength(180)
            manual.addWidget(self.printer_name_input, 1)
            self.printer_address_input = QtWidgets.QLineEdit()
            self.printer_address_input.setPlaceholderText("http://192.168.0.20:7125")
            self.printer_address_input.setMaxLength(1024)
            manual.addWidget(self.printer_address_input, 2)
            self.printer_backend_input = QtWidgets.QComboBox()
            for label, backend in [("자동 확인", "auto"), ("OctoPrint", "octoprint"), ("Moonraker / Klipper", "moonraker"), ("PreForm API", "formlabs")]:
                self.printer_backend_input.addItem(label, backend)
            manual.addWidget(self.printer_backend_input)
            self.printer_add_button = QtWidgets.QPushButton("등록 · 확인")
            self.printer_add_button.clicked.connect(self.add_network_printer)
            manual.addWidget(self.printer_add_button)
            layout.addWidget(group)
            row = QtWidgets.QHBoxLayout()
            button = QtWidgets.QPushButton("모델을 슬라이서에서 준비")
            button.clicked.connect(self.open_slicer)
            row.addWidget(button)
            self.printer_file_button = QtWidgets.QPushButton("준비한 G-code 선택")
            self.printer_file_button.clicked.connect(self.choose_printer_file)
            row.addWidget(self.printer_file_button)
            self.printer_send_button = QtWidgets.QPushButton("출력 파일 전송")
            self.printer_send_button.setObjectName("primary")
            self.printer_send_button.clicked.connect(self.upload_printer_file)
            row.addWidget(self.printer_send_button)
            layout.addLayout(row)
            self.printer_file_label = QtWidgets.QLabel("전송 파일 없음 · STL / 3MF는 슬라이서에서 장비용 파일로 변환하세요.")
            self.printer_file_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            self.printer_file_label.setWordWrap(True)
            layout.addWidget(self.printer_file_label)
            note = QtWidgets.QLabel("직접 전송: OctoPrint·Moonraker의 텍스트 G-code. 레진 프린터 전용 파일은 제조사 슬라이서에서 전송하세요. 이 화면의 전송은 출력 시작을 요청하지 않습니다.")
            note.setObjectName("hint")
            note.setWordWrap(True)
            layout.addWidget(note)
            self.printer_status_label = QtWidgets.QLabel("검색 대기")
            self.printer_status_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            self.printer_status_label.setWordWrap(True)
            layout.addWidget(self.printer_status_label)
            button = QtWidgets.QPushButton("선택한 프린터 사용 · 닫기")
            button.clicked.connect(dialog.close)
            layout.addWidget(button)
            self.printer_timer = QtCore.QTimer(dialog)
            self.printer_timer.setInterval(2000)
            self.printer_timer.timeout.connect(self.refresh_printer_table)
            self.printer_timer.start()
            self.refresh_printer_table()
            QtCore.QTimer.singleShot(0, self.discover_printers)
        self.printer_dialog.show()
        self.printer_dialog.raise_()
        self.printer_dialog.activateWindow()
        self.refresh_printer_controls()

    def save_printer_selection(self):
        settings = QtCore.QSettings("EXOCADE", "DentalDesign")
        profiles = []
        for profile in self.printers.values():
            if profile.get("manual") or profile.get("id") == self.selected_printer_id:
                profiles.append({field: profile[field] for field in ("id", "name", "model", "source", "connection", "address", "backend", "device_id", "serial", "manual", "is_3d_candidate", "reason") if field in profile})
        settings.setValue("printer_profiles", json.dumps(profiles, ensure_ascii=False))
        settings.setValue("selected_printer_id", self.selected_printer_id)

    def refresh_printer_table(self, *args):
        if self.printer_dialog is None:
            return
        table = self.printer_table
        table.blockSignals(True)
        table.setRowCount(0)
        selected_row = -1
        for profile in sorted(self.printers.values(), key=lambda item: (not item.get("is_3d_candidate", False), item.get("name", "").casefold())):
            identity = profile["id"]
            if not self.printer_show_other.isChecked() and not profile.get("is_3d_candidate") and identity != self.selected_printer_id:
                continue
            check = self.printer_checks.get(identity, {})
            fresh = time.monotonic()-check.get("checked_at", -1000) < 60
            state = {"ready": "대기 · 준비됨", "printing": "출력 중", "paused": "일시 정지", "offline": "연결 안 됨", "connecting": "연결 준비 중", "connected": "연결됨", "error": "장비 오류", "unknown": "장비 상태 미확인"}.get(check.get("machine_state"), "장비 상태 미확인")
            status = state if fresh else "연결 재확인 필요" if check else profile.get("status", "연결 확인 필요")
            backend = check.get("backend", profile.get("backend"))
            support = "G-code 전송 연동" if backend in {"octoprint", "moonraker"} else "상태 조회 · PreForm 사용" if backend == "formlabs" else "연동 방식 확인 필요"
            row = table.rowCount()
            table.insertRow(row)
            for column, value in enumerate([profile.get("name", "프린터"), profile.get("connection", ""), profile.get("address", ""), status, support]):
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setData(QtCore.Qt.ItemDataRole.UserRole, identity)
                table.setItem(row, column, item)
            if identity == self.selected_printer_id:
                selected_row = row
        if selected_row >= 0:
            table.setCurrentCell(selected_row, 0)
        table.blockSignals(False)
        self.refresh_printer_controls()

    def refresh_printer_controls(self):
        profile = self.printers.get(self.selected_printer_id)
        check = self.printer_checks.get(self.selected_printer_id, {})
        fresh = bool(check) and time.monotonic()-check.get("checked_at", -1000) < 60
        busy = self._printer_discovery is not None or self._printer_request is not None
        summary = "선택한 프린터 없음" if not profile else "선택: "+profile.get("name", "프린터")
        if profile:
            summary += " · "+("연결 확인됨" if fresh and check.get("connected") else "연결 확인 필요")
        self.selected_printer_label.setText(summary)
        if self.printer_dialog is None:
            return
        self.printer_search_button.setEnabled(not busy)
        self.printer_network.setEnabled(not busy)
        self.printer_add_button.setEnabled(not busy)
        self.printer_cancel_button.setEnabled(busy)
        self.printer_table.setEnabled(not busy)
        self.printer_show_other.setEnabled(not busy)
        self.printer_key.setEnabled(bool(profile) and not busy)
        self.printer_check_button.setEnabled(bool(profile) and not busy)
        self.printer_remove_button.setEnabled(bool(profile and profile.get("manual")) and not busy)
        self.printer_file_button.setEnabled(bool(profile) and not busy)
        self.printer_send_button.setEnabled(bool(profile and fresh and check.get("success") and check.get("ready") and check.get("upload_supported") and check.get("backend") in {"octoprint", "moonraker"} and self._printer_job_target_id == self.selected_printer_id and self._printer_job_path and Path(self._printer_job_path).is_file()) and not busy)
        if profile:
            detail = profile.get("reason", "")
            if check:
                detail = check.get("message", detail)
                if not fresh:
                    detail += " · 연결 상태를 다시 확인하세요."
            self.printer_detail.setText(summary+"\n"+detail)
        else:
            self.printer_detail.setText("검색된 3D 프린터가 없으면 연결 상태를 확인하거나 IP 주소를 직접 등록하세요. 기타 장치에는 일반 프린터와 미확인 COM 포트가 포함됩니다.")

    def printer_row_changed(self, row, column, previous_row, previous_column):
        if row < 0 or self.printer_table.item(row, 0) is None:
            return
        identity = self.printer_table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
        if identity != self.selected_printer_id:
            self._printer_job_path = ""
            self._printer_job_target_id = ""
            self.printer_file_label.setText("전송 파일 없음 · 선택한 프린터의 슬라이서 설정으로 준비한 파일을 선택하세요.")
        self.selected_printer_id = identity
        self.printer_key.setText(self.printer_api_keys.get(identity, ""))
        self.save_printer_selection()
        self.refresh_printer_controls()

    def printer_key_changed(self, text):
        if self.selected_printer_id:
            self.printer_api_keys[self.selected_printer_id] = text
            self.printer_checks.pop(self.selected_printer_id, None)
            self.refresh_printer_table()

    def discover_printers(self):
        if self._printer_discovery is not None or self._printer_request is not None:
            return
        self.printer_status_label.setText("장치를 검색하고 있습니다…")
        worker = PrinterDiscoveryWorker(self, include_network=self.printer_network.isChecked())
        self._printer_discovery = worker
        worker.devices_found.connect(self.printer_discovery_result)
        worker.finished.connect(self.printer_discovery_finished)
        self.refresh_printer_controls()
        worker.start()

    def printer_discovery_result(self, devices, errors):
        retained = {identity: profile for identity, profile in self.printers.items() if profile.get("manual") or identity == self.selected_printer_id}
        found_ids = set()
        for profile in devices:
            identity = profile.get("id")
            if identity:
                retained[identity] = dict(profile)
                found_ids.add(identity)
        for identity, profile in retained.items():
            if identity not in found_ids and not profile.get("manual"):
                profile["status"] = "이전 선택 · 이번 검색에서 발견되지 않음"
        self.printers = retained
        self.printer_checks.clear()
        count = sum(bool(item.get("is_3d_candidate")) for item in devices)
        message = f"검색 완료 · 3D 프린터 후보 {count}대 · 기타 장치 {len(devices)-count}대"
        if count == 0:
            message += " · 검색된 3D 프린터가 없습니다."
        if errors:
            message += "\n"+" / ".join(str(error) for error in errors)
        self.printer_status_label.setText(message)
        self.save_printer_selection()
        self.refresh_printer_table()

    def printer_discovery_finished(self):
        worker = self._printer_discovery
        self._printer_discovery = None
        if worker is not None:
            worker.deleteLater()
        self.refresh_printer_controls()
        if self._printer_closing and self._printer_request is None:
            QtCore.QTimer.singleShot(0, self.close)

    def add_network_printer(self):
        address = self.printer_address_input.text().strip()
        if "://" not in address:
            address = "http://"+address
        try:
            parsed = urllib.parse.urlsplit(address)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or any(character.isspace() or ord(character) < 32 for character in address) or "\\" in address or (parsed.port is not None and not 1 <= parsed.port <= 65535):
                raise ValueError()
        except ValueError:
            self.printer_status_label.setText("프린터의 HTTP 또는 HTTPS 주소를 입력하세요. API 키는 별도 입력란을 사용하세요.")
            return
        address = address.rstrip("/")
        backend = self.printer_backend_input.currentData()
        identity = next((identity for identity, item in self.printers.items() if item.get("manual") and item.get("address") == address and item.get("backend") == backend), "manual:"+uuid.uuid4().hex)
        self.printers[identity] = {"id": identity, "name": self.printer_name_input.text().strip() or parsed.hostname, "source": "직접 등록", "connection": "네트워크", "address": address, "backend": backend, "manual": True, "is_3d_candidate": True, "status": "연결 확인 필요", "reason": "등록한 주소에서 지원 API와 프린터 상태를 확인하세요."}
        self.selected_printer_id = identity
        self.printer_checks.pop(identity, None)
        self.printer_key.setText(self.printer_api_keys.get(identity, ""))
        self._printer_job_path = ""
        self._printer_job_target_id = ""
        self.printer_file_label.setText("전송 파일 없음 · 선택한 프린터의 슬라이서 설정으로 준비한 파일을 선택하세요.")
        self.save_printer_selection()
        self.refresh_printer_table()
        self.check_printer()

    def remove_printer(self):
        profile = self.printers.get(self.selected_printer_id, {})
        if not profile.get("manual"):
            return
        self.printer_api_keys.pop(self.selected_printer_id, None)
        self.printer_checks.pop(self.selected_printer_id, None)
        self.printers.pop(self.selected_printer_id, None)
        self.selected_printer_id = ""
        self._printer_job_path = ""
        self._printer_job_target_id = ""
        self.printer_key.clear()
        self.printer_file_label.setText("전송 파일 없음")
        self.save_printer_selection()
        self.refresh_printer_table()

    def check_printer(self):
        self.start_printer_request("check")

    def choose_printer_file(self):
        if not self.selected_printer_id:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self.printer_dialog, "선택한 프린터용 G-code", "", "텍스트 G-code (*.gcode)")
        if path:
            candidate = Path(path)
            if candidate.suffix.lower() != ".gcode" or not candidate.is_file():
                self.printer_status_label.setText("슬라이서에서 준비한 .gcode 파일을 선택하세요.")
                return
            self._printer_job_path = str(candidate.resolve())
            self._printer_job_target_id = self.selected_printer_id
            self.printer_file_label.setText("전송 파일: "+self._printer_job_path)
            self.refresh_printer_controls()

    def upload_printer_file(self):
        self.refresh_printer_controls()
        if self.printer_send_button.isEnabled():
            self.start_printer_request("upload")

    def start_printer_request(self, mode):
        if self._printer_discovery is not None or self._printer_request is not None:
            return
        profile = self.printers.get(self.selected_printer_id)
        if not profile:
            return
        key = self.printer_api_keys.get(self.selected_printer_id, "")
        self._printer_request_identity = self.selected_printer_id
        self._printer_request_profile = dict(profile)
        self._printer_request_key = key
        self.printer_checks.pop(self.selected_printer_id, None)
        worker = PrinterConnectionWorker(profile, mode=mode, file_path=self._printer_job_path if mode == "upload" else "", api_key=key, parent=self)
        self._printer_request = worker
        worker.result.connect(self.printer_request_result)
        worker.failed.connect(self.printer_request_failed)
        worker.progress.connect(self.printer_status_label.setText)
        worker.finished.connect(self.printer_request_finished)
        self.printer_status_label.setText("프린터 연결을 확인하고 있습니다…")
        self.refresh_printer_controls()
        worker.start()

    def printer_request_result(self, result):
        identity = self._printer_request_identity
        if self.printers.get(identity) != self._printer_request_profile or self.printer_api_keys.get(identity, "") != self._printer_request_key:
            return
        if result.get("mode") == "check":
            result = dict(result)
            result["checked_at"] = time.monotonic()
            self.printer_checks[identity] = result
        else:
            self.printer_checks.pop(identity, None)
        self.printer_status_label.setText(result.get("message", "상태를 확인했습니다."))
        self.refresh_printer_table()

    def printer_request_failed(self, message):
        self.printer_checks.pop(self._printer_request_identity, None)
        self.printer_status_label.setText(message)
        self.refresh_printer_table()

    def printer_request_finished(self):
        worker = self._printer_request
        self._printer_request = None
        self._printer_request_key = ""
        if worker is not None:
            worker.deleteLater()
        self.refresh_printer_controls()
        if self._printer_closing and self._printer_discovery is None:
            QtCore.QTimer.singleShot(0, self.close)

    def cancel_printer_task(self):
        for worker in (self._printer_discovery, self._printer_request):
            if worker is not None:
                worker.requestInterruption()
        self.printer_status_label.setText("작업 취소를 요청했습니다. 응답을 기다리고 있습니다…")
        self.printer_cancel_button.setEnabled(False)

    def closeEvent(self, event):
        if self._printer_discovery is not None or self._printer_request is not None:
            self._printer_closing = True
            self.cancel_printer_task()
            self.statusBar().showMessage("프린터 작업을 취소한 뒤 종료합니다.")
            event.ignore()
            return
        self._printer_closing = False
        if self.dirty:
            result = QtWidgets.QMessageBox.question(self, "종료", "현재 변경사항을 저장하시겠습니까?",
                QtWidgets.QMessageBox.StandardButton.Save | QtWidgets.QMessageBox.StandardButton.Discard | QtWidgets.QMessageBox.StandardButton.Cancel)
            if result == QtWidgets.QMessageBox.StandardButton.Cancel or (result == QtWidgets.QMessageBox.StandardButton.Save and not self.save_project()):
                event.ignore()
                return
        self.orientation.SetEnabled(0)
        self.printer_api_keys.clear()
        self.vtk_widget.Finalize()
        event.accept()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("EXOCADE")
    app.setOrganizationName("EXOCADE")
    app.setStyle("Fusion")
    asset_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "assets"
    app.setStyleSheet((asset_dir / "theme.qss").read_text(encoding="utf-8"))
    sys.excepthook = lambda error_type, error, tb: QtWidgets.QMessageBox.critical(None, "EXOCADE 오류", str(error))
    window = DentalWindow()
    window.show()
    sys.exit(app.exec())
