"""Train operator codes: LINX ResponsibleRU codes and Darwin/CIF TOC codes."""

# LINX ResponsibleRU codes, identified from the classes each one runs
LINX_OPERATOR_NAMES = {
    "9907": "Caledonian Sleeper", "9920": "TransPennine Express", "9921": "Greater Anglia",
    "9922": "Grand Central", "9923": "Northern", "9925": "Great Western Railway", "9927": "CrossCountry",
    "9928": "East Midlands Railway", "9929": "West Midlands Trains", "9933": "Elizabeth line",
    "9945": "Lumo", "9955": "Hull Trains", "9960": "ScotRail", "9961": "LNER", "9964": "Merseyrail",
    "9965": "Avanti West Coast", "9971": "Transport for Wales", "9974": "Chiltern Railways",
    "9980": "Southeastern", "9984": "South Western Railway", "9985": "Island Line",
    "9986": "Heathrow Express", "9988": "Govia Thameslink Railway",
}

# Darwin/CIF TOC code -> LINX code (Govia Thameslink runs several brands under one LINX code)
LINX_CODE_FOR_TOC = {
    "CS": "9907", "TP": "9920", "LE": "9921", "GC": "9922", "NT": "9923", "GW": "9925", "XC": "9927",
    "EM": "9928", "LM": "9929", "XR": "9933", "LD": "9945", "HT": "9955", "SR": "9960", "GR": "9961",
    "ME": "9964", "VT": "9965", "AW": "9971", "CH": "9974", "SE": "9980", "SW": "9984", "IL": "9985",
    "HX": "9986", "TL": "9988", "GN": "9988", "SN": "9988", "GX": "9988",
}

TOC_NAMES = {
    "SW": "South Western Railway", "GW": "Great Western Railway", "SN": "Southern", "TL": "Thameslink",
    "GN": "Great Northern", "GX": "Gatwick Express", "LO": "London Overground", "XR": "Elizabeth line",
    "SE": "Southeastern", "LE": "Greater Anglia", "NT": "Northern", "TP": "TransPennine Express",
    "XC": "CrossCountry", "EM": "East Midlands Railway", "LM": "West Midlands Trains", "SR": "ScotRail",
    "GR": "LNER", "VT": "Avanti West Coast", "AW": "Transport for Wales", "CH": "Chiltern Railways",
    "CC": "c2c", "HX": "Heathrow Express", "GC": "Grand Central", "HT": "Hull Trains", "LD": "Lumo",
    "ME": "Merseyrail", "IL": "Island Line", "CS": "Caledonian Sleeper",
    "ZZ": "Freight / other", "LS": "Locomotive Services", "GB": "GB Railfreight", "DB": "DB Cargo",
    "FL": "Freightliner", "NR": "Network Rail", "DR": "Direct Rail Services", "CR": "Colas Rail",
    "RT": "Rail Operations Group", "WR": "West Coast Railway Company", "PT": "Europorte / GB Railfreight",
    "LR": "Network Rail (infrastructure)",
}
