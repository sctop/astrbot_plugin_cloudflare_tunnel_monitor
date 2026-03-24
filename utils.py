import datetime
import json
from typing import Dict, List, Any
from zoneinfo import ZoneInfo
from _pydatetime import tzinfo


class TunnelStatusUtils:
    @staticmethod
    def calc_status_difference(old: dict, new: dict) -> dict[str, list["TunnelStatusModel"]]:
        # status change (healthy, degraded, down)
        status_difference = {
            "to_healthy": [],
            "to_degraded": [],
            "to_down": [],
            "conn_changed": [],
        }

        old_dict = old
        new_dict = new

        for (uuid, old_obj) in old_dict.items():
            new_obj = new_dict[uuid]

            if old_obj == new_obj:
                continue
            else:
                if old_obj.status != 'healthy' and new_obj.status == 'healthy':
                    status_difference["to_healthy"].append(uuid)
                elif old_obj.status != 'degraded' and new_obj.status == 'degraded':
                    status_difference["to_degraded"].append(uuid)
                elif old_obj.status != 'down' and new_obj.status == 'down':
                    status_difference["to_down"].append(uuid)
                elif old_obj.status == 'healthy' and new_obj.status == 'healthy' and old_obj.conns_active_at != new_obj.conns_active_at:
                    status_difference['to_healthy'].append(uuid)
                elif old_obj.name != new_obj.name:
                    # useless case: name changes doesn't matter
                    continue
                else:
                    status_difference["conn_changed"].append(uuid)

        return status_difference

    @staticmethod
    def find_deleted_tunnels(old: list, new: list) -> list:
        old_uuids = set([i.id for i in old])
        new_uuids = set([i.id for i in new])
        diff = old_uuids - new_uuids

        return list(diff)

    @staticmethod
    def pair_umo_and_tunnelid_by_tunnel_ids(tunnel_to_umo: Dict[str, List[str]],
                                            tunnel_ids: list,
                                            excluded_umos=None) -> Dict[str, List[str]]:
        if excluded_umos is None:
            excluded_umos = []

        result = {}
        for id in tunnel_ids:
            for umo in tunnel_to_umo[id]:
                if umo in excluded_umos:
                    continue

                if umo not in result:
                    result[umo] = []
                result[umo].append(id)

        return result


class TimeUtils:
    @staticmethod
    def get_current_strftime_by_timezone(timezone: str):
        current = datetime.datetime.now(tz=ZoneInfo(timezone))
        return current.strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def get_current_strftime_utc(cls):
        return cls.get_current_strftime_by_timezone('UTC')

    @staticmethod
    def get_ddhhmmss_from_seconds(seconds: int | float) -> str:
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f'{int(days)}天{int(hours):02d}时{int(minutes):02d}分{int(seconds):02d}秒'

    @staticmethod
    def get_datetime_strftime_in_tz(dt: datetime.datetime, tz: ZoneInfo | str) -> str:
        return dt.astimezone(tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S")


class FileUtils:
    @staticmethod
    def load_json_with_default(filepath: str, default: Any) -> Any:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(default, f)
            return default
