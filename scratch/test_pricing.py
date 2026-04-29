import requests, json

def test_price_anywhere(sku):
    filter_str = f"skuName eq '{sku}' and priceType eq 'Consumption'"
    url = "https://prices.azure.com/api/retail/prices"
    print(f"URL: {url}?$filter={filter_str}")
    
    r = requests.get(url, params={"$filter": filter_str})
    data = r.json()
    items = data.get("Items", [])
    print(f"Found {len(items)} items")
    for item in items[:10]:
        print(f"- {item.get('productName')} | {item.get('skuName')} | {item.get('meterName')} | {item.get('retailPrice')} | {item.get('armRegionName')}")

test_price_anywhere("DC1ds v3")


