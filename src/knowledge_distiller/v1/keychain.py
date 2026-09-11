from __future__ import annotations

import ctypes
import ctypes.util
import sys
from dataclasses import dataclass
from functools import lru_cache


ERR_SEC_SUCCESS = 0
ERR_SEC_DUPLICATE_ITEM = -25299
ERR_SEC_ITEM_NOT_FOUND = -25300


class _Attribute(ctypes.Structure):
    _fields_ = [("tag", ctypes.c_uint32), ("length", ctypes.c_uint32), ("data", ctypes.c_void_p)]


class _AttributeList(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("attr", ctypes.POINTER(_Attribute))]


def credential_label(account: str, state: str = "已保存", detail: str = "") -> str:
    purpose = next((name for prefix, name in (
        ("seed_draft_api_account-", "豆包语音 API"),
        ("seed_draft_tos_account-", "TOS 存储凭据"),
        ("llm_draft_secret_account-", "语言模型 API"),
        ("llm-api-key-", "语言模型 API"),
    ) if account.startswith(prefix)), "连接凭据")
    return "｜".join(filter(None, ("知识蒸馏器", purpose, state, detail, account[-8:])))


class KeychainError(RuntimeError):
    pass


class MacOSKeychain:
    def __init__(self) -> None:
        security_path = ctypes.util.find_library("Security")
        core_path = ctypes.util.find_library("CoreFoundation")
        if not security_path or not core_path:
            raise RuntimeError("macos_keychain_unavailable")

        self.security = ctypes.CDLL(security_path)
        self.core = ctypes.CDLL(core_path)
        self._configure_functions()

    def _configure_functions(self) -> None:
        void = ctypes.c_void_p
        uint = ctypes.c_uint32
        pointer_void = ctypes.POINTER(void)
        pointer_uint = ctypes.POINTER(uint)

        self.security.SecKeychainAddGenericPassword.argtypes = [
            void,
            uint,
            void,
            uint,
            void,
            uint,
            void,
            pointer_void,
        ]
        self.security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainFindGenericPassword.argtypes = [
            void,
            uint,
            void,
            uint,
            void,
            pointer_uint,
            pointer_void,
            pointer_void,
        ]
        self.security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainItemModifyAttributesAndData.argtypes = [
            void,
            void,
            uint,
            void,
        ]
        self.security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self.security.SecKeychainItemDelete.argtypes = [void]
        self.security.SecKeychainItemDelete.restype = ctypes.c_int32
        self.security.SecKeychainItemFreeContent.argtypes = [void, void]
        self.security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self.core.CFRelease.argtypes = [void]
        self.core.CFRelease.restype = None

    @staticmethod
    def _buffer(value: str) -> tuple[bytes, ctypes.Array[ctypes.c_char]]:
        encoded = value.encode("utf-8")
        return encoded, ctypes.create_string_buffer(encoded)

    def _find(
        self,
        service: str,
        account: str,
    ) -> tuple[int, bytes | None, ctypes.c_void_p]:
        service_bytes, service_buffer = self._buffer(service)
        account_bytes, account_buffer = self._buffer(account)
        password_length = ctypes.c_uint32()
        password_data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None,
            len(service_bytes),
            ctypes.cast(service_buffer, ctypes.c_void_p),
            len(account_bytes),
            ctypes.cast(account_buffer, ctypes.c_void_p),
            ctypes.byref(password_length),
            ctypes.byref(password_data),
            ctypes.byref(item),
        )
        if status != ERR_SEC_SUCCESS:
            return status, None, item

        try:
            value = ctypes.string_at(password_data, password_length.value)
        finally:
            self.security.SecKeychainItemFreeContent(None, password_data)
        return status, value, item

    def save(self, service: str, account: str, value: str) -> int:
        service_bytes, service_buffer = self._buffer(service)
        account_bytes, account_buffer = self._buffer(account)
        value_bytes, value_buffer = self._buffer(value)
        item = ctypes.c_void_p()
        status = self.security.SecKeychainAddGenericPassword(
            None,
            len(service_bytes),
            ctypes.cast(service_buffer, ctypes.c_void_p),
            len(account_bytes),
            ctypes.cast(account_buffer, ctypes.c_void_p),
            len(value_bytes),
            ctypes.cast(value_buffer, ctypes.c_void_p),
            ctypes.byref(item),
        )
        if item.value:
            self.core.CFRelease(item)
        if status != ERR_SEC_DUPLICATE_ITEM:
            return status

        find_status, _, found_item = self._find(service, account)
        if find_status != ERR_SEC_SUCCESS:
            return find_status
        try:
            return self.security.SecKeychainItemModifyAttributesAndData(
                found_item,
                None,
                len(value_bytes),
                ctypes.cast(value_buffer, ctypes.c_void_p),
            )
        finally:
            self.core.CFRelease(found_item)

    def set_label(self, service: str, account: str, label: str) -> int:
        service_bytes, service_buffer = self._buffer(service)
        account_bytes, account_buffer = self._buffer(account)
        item = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None, len(service_bytes), ctypes.cast(service_buffer, ctypes.c_void_p),
            len(account_bytes), ctypes.cast(account_buffer, ctypes.c_void_p),
            None, None, ctypes.byref(item),
        )
        if status != ERR_SEC_SUCCESS:
            return status
        try:
            encoded, buffer = self._buffer(label)
            attribute = _Attribute(int.from_bytes(b"labl", "big"), len(encoded), ctypes.cast(buffer, ctypes.c_void_p))
            attributes = _AttributeList(1, ctypes.pointer(attribute))
            return self.security.SecKeychainItemModifyAttributesAndData(item, ctypes.byref(attributes), 0, None)
        finally:
            self.core.CFRelease(item)

    def load(self, service: str, account: str) -> tuple[int, str | None]:
        status, value, item = self._find(service, account)
        if item.value:
            self.core.CFRelease(item)
        if status != ERR_SEC_SUCCESS or value is None:
            return status, None
        try:
            return status, value.decode("utf-8")
        except UnicodeDecodeError:
            return status, None

    def clear(self, service: str, account: str) -> int:
        status, _, item = self._find(service, account)
        if status != ERR_SEC_SUCCESS:
            return status
        try:
            return self.security.SecKeychainItemDelete(item)
        finally:
            self.core.CFRelease(item)


@lru_cache(maxsize=1)
def system_keychain():
    if sys.platform == 'win32':
        from .windows_credentials import WindowsKeychain
        return WindowsKeychain()
    return MacOSKeychain()


@dataclass(frozen=True)
class KeychainSecret:
    account: str
    service: str = "com.knowledge-distiller.credentials"

    def save(self, value: str) -> None:
        if not value or "\n" in value or "\r" in value:
            raise ValueError("secret is empty or multiline")
        if system_keychain().save(self.service, self.account, value) != ERR_SEC_SUCCESS:
            raise KeychainError("keychain_save_failed")

    def set_label(self, label: str) -> None:
        if system_keychain().set_label(self.service, self.account, label) != ERR_SEC_SUCCESS:
            raise KeychainError("keychain_label_failed")

    def load(self) -> str:
        status, value = system_keychain().load(self.service, self.account)
        if status != ERR_SEC_SUCCESS or not value:
            raise KeychainError("keychain_secret_unavailable")
        return value

    def clear(self) -> None:
        status = system_keychain().clear(self.service, self.account)
        if status not in {ERR_SEC_SUCCESS, ERR_SEC_ITEM_NOT_FOUND}:
            raise KeychainError("keychain_clear_failed")
