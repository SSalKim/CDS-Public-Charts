"""Model routing across two verified Drives, with one publication coordinator."""
import copy
import os

from public_charts_drive import ChartDriveError, ChartDriveStore


LAYOUT = "balanced-v1"
ROUTES = {"gfs": "primary", "cmc": "primary", "ecmwf": "primary",
          "aifs": "secondary", "aigfs": "secondary", "icon": "secondary", "ukmo": "primary"}


def balanced_enabled():
    return os.environ.get("PUBLIC_CHARTS_STORAGE_LAYOUT") == LAYOUT


def production_store():
    if not balanced_enabled():
        return ChartDriveStore.from_environment()
    return ChartDrivePool.from_environment()


class ChartDrivePool:
    """Private state stays on primary; image ownership is discovered, never guessed."""

    def __init__(self, stores):
        if set(stores) != {"primary", "secondary"}:
            raise ChartDriveError("Both explicitly configured chart Drives are required.")
        self.stores = dict(stores)
        primary, secondary = self.stores["primary"], self.stores["secondary"]
        if primary.account_email == secondary.account_email or primary.folder_id == secondary.folder_id:
            raise ChartDriveError("Chart Drives must be different accounts and folders.")
        self.folder_id, self.account_email = primary.folder_id, primary.account_email
        self.storage_folders = {name: store.folder_id for name, store in self.stores.items()}
        self._file_accounts = {}

    @classmethod
    def from_environment(cls):
        return cls({"primary": ChartDriveStore.from_environment(),
                    "secondary": ChartDriveStore.from_environment("PUBLIC_CHARTS_DRIVE2_")})

    def verify_account_and_folder(self):
        for store in self.stores.values():
            store.verify_account_and_folder()

    def quota_summary(self):
        return {account: store.storage_quota() for account, store in self.stores.items()}

    @staticmethod
    def account_for_key(key):
        ChartDriveStore._validate_key(key)
        return ROUTES[key.split("/", 1)[0]]

    def account_for_file(self, file_id):
        try:
            return self._file_accounts[file_id]
        except KeyError:
            raise ChartDriveError("Image account is absent from the verified inventory.") from None

    def inventory(self):
        result, owners = [], {}
        for account, store in self.stores.items():
            for item in store.inventory():
                if item["id"] in owners:
                    raise ChartDriveError("A managed file occurs in both chart Drives.")
                owners[item["id"]] = account
                result.append({**item, "storage_account": account})
        self._file_accounts = owners
        return result

    def prime_inventory(self, inventory):
        for account, store in self.stores.items():
            store.prime_inventory([item for item in inventory if item.get("storage_account") == account])

    def upload_image(self, path, key):
        account = self.account_for_key(key)
        result = self.stores[account].upload_image(path, key)
        self._file_accounts[result["file_id"]] = account
        return {**result, "storage_account": account}

    def new_upload_worker(self):
        worker = type(self)({name: store.new_upload_worker() for name, store in self.stores.items()})
        worker._file_accounts = copy.copy(self._file_accounts)
        return worker

    def read_json(self, name, default=None):
        return self.stores["primary"].read_json(name, default)

    def write_json(self, name, payload):
        return self.stores["primary"].write_json(name, payload)

    def delete_managed(self, file_id):
        account = self.account_for_file(file_id)
        self.stores[account].delete_managed(file_id)
