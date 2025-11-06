import pandas as pd
import boto3
import os
import sys
from botocore.exceptions import ClientError

PROFILE = "test"
REGION = "ap-southeast-1"
INDEX = "PetSleepWell"


session = boto3.Session(profile_name=PROFILE, region_name=REGION)

print("[debug] AWS_PROFILE (env):", os.getenv("AWS_PROFILE"))
print("[debug] session.region   :", session.region_name)

sts = session.client("sts")
ident = sts.get_caller_identity()
print("[debug] account/arn      :", ident["Account"], ident["Arn"])

loc = session.client("location")  # 已經綁定 REGION

# 先 sanity check：Describe + 列出確保有這個 Index
try:
    desc = loc.describe_place_index(IndexName=INDEX)
    print("[debug] describe ok; data source:", desc["DataSource"],
          "intendedUse:", desc["DataSourceConfiguration"]["IntendedUse"])
except ClientError as e:
    print("❌ Describe 失敗：", e)
    sys.exit(2)

client = session.client("location")


def geocode(address: str):
    """呼叫 ALS Place Index，回傳 (lat, lng, status)"""
    try:
        resp = client.search_place_index_for_text(
            IndexName=INDEX,
            Text=address,
            MaxResults=1,
            FilterCountries=["TWN"]
        )
        items = resp.get("Results", [])
        if not items:
            return None, None, "NO_RESULT"
        lng, lat = items[0]["Place"]["Geometry"]["Point"]
        return lat, lng, "OK"
    except Exception as e:
        return None, None, str(e)


if __name__ == "__main__":
    filename = "寵物殯葬業者聯絡資訊.csv"
    df = pd.read_csv(filename)
    df = df.ffill()

    # results = df["地址"].apply(geocode)
    # df[["lat", "lng", "status"]] = pd.DataFrame(results.tolist(), index=df.index)

    df[["名稱", "電話", "地址", "網址", "mail", "LINE", "備註", "lat",
        "lng", "status"]].to_csv(filename, index=False)
