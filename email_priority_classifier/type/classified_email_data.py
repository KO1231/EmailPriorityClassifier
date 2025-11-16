import base64
import json

_LABEL_REPLACE_DATA = {
    "INBOX": "Inbox",
    "CATEGORY_PERSONAL": "Category: Personal",  # 他のタブに分類されない個人的な会話やメール。
    "CATEGORY_PROMOTIONS": "Category: Promotions",  # マーケティング、関心のあるトピック、社会的・政治的運動などに関するプロモーション メール。
    "CATEGORY_FORUMS": "Category: Forums",  # オンライン グループ、掲示板、メーリング リストからのメール。
    "CATEGORY_UPDATES": "Category: Updates",  # 新着の自分宛の自動生成メール（確認書、領収書、請求書、明細書など）。
    "CATEGORY_SOCIAL": "Category: Social",  # ソーシャル ネットワーク、メディア共有サイト、その他のソーシャル ウェブサイトからのメール。
    "IMPORTANT": "IMPORTANT",
    "SPAM": "SPAM",
    "YELLOW_STAR": "Starred",
    "TRASH": "Trash",
}


class ClassifiedEmailData:
    @staticmethod
    def _PARSE_LABEL(raw_labels: list[str], personal_labels_info: dict[str, str]) -> list[str]:
        parsed_labels = []
        for label in raw_labels:
            if label in _LABEL_REPLACE_DATA:
                parsed_labels.append(_LABEL_REPLACE_DATA[label])
            elif label in personal_labels_info:
                parsed_labels.append(personal_labels_info[label])
            # 含まれていない場合(システムラベルの一部など)は除外
        return parsed_labels

    @staticmethod
    def _extract_subject_from_payload(payload: dict) -> str:
        headers = payload.get("headers", [])
        for header in headers:
            if header.get("name", "").lower() == "subject":
                return header.get("value", "")
        return ""

    @staticmethod
    def _decode_body(body: str, headers: list[dict]) -> str:
        return (base64.urlsafe_b64decode(body).decode("utf-8")
                .replace("\r", " ").replace("\n", " ").replace("\t", " "))
        """
        for header in headers:
            if header.get("name", "").lower() == 'content-transfer-encoding':
                encoding = header.get("value").lower()
                if encoding == "base64":
                    return base64.urlsafe_b64decode(body).decode("utf-8")
                elif encoding == "quoted-printable":
                    return quopri.decodestring(body).decode("utf-8")
        return body
        """

    def __init__(self, date: int, payload: dict, size_estimate: int, labels: list[str]):
        self.date = date
        self.payload = payload
        self.subject = self._extract_subject_from_payload(payload)
        self.size_estimate = size_estimate
        self.labels = labels

    def get_data(self) -> str:
        payload = self.payload
        body = payload.get("body", {})
        if len(body) != 0 and body.get("size", 0) != 0:
            data = self._decode_body(body["data"], payload.get("headers", []))
            return data

        text_parts = [part for part in payload.get("parts", []) if part.get("mimeType", "").startswith("text/")]
        if len(text_parts) == 0:
            return "parts could not found."

        for text_part in text_parts:
            mimetype = text_part["mimeType"]
            if mimetype == "text/plain" or mimetype == "text/html":
                # 全体のmimetypeがtext/plainかtext/htmlの場合、そのpartを返す
                body_data = text_part.get("body", {}).get("data")
                if body_data is None:
                    return "body data could not found."
                data = self._decode_body(text_part["body"]["data"], text_part.get("headers", []))
                return data

        return json.dumps(text_parts[0], ensure_ascii=False)

    @classmethod
    def init(cls, message: dict, personal_labels_info: dict[str, str]) -> "ClassifiedEmailData":
        return cls(
            date=int(message.get("internalDate", "0")),
            payload=message.get("payload", {}),
            size_estimate=int(message.get("sizeEstimate", "-1")),
            labels=cls._PARSE_LABEL(message.get("labelIds", []), personal_labels_info)
        )


class ClassifiedEmailDataEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, ClassifiedEmailData):
            return {
                "date": o.date,
                "size_estimate": o.size_estimate,
                "data": o.get_data(),
            }
        return super().default(o)
