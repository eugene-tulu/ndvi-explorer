import streamlit as st
import streamlit.components.v1 as components
from streamlit_folium import st_folium
import folium
from shapely.geometry import shape
import geopandas as gpd
import pystac_client
import stackstac
import numpy as np
import xarray as xr
import rioxarray
from dask.diagnostics import ProgressBar
import plotly.express as px
import tempfile
import os
from datetime import datetime
from folium import plugins
import warnings

warnings.filterwarnings("ignore")

# Constants
# Constants
MAX_AREA_KM2 = 500
GA_MEASUREMENT_ID = st.secrets.get("GA_MEASUREMENT_ID", None)

if GA_MEASUREMENT_ID:
    st.html(f"""
    <script async src="https://www.googletagmanager.com/gtag/js?id={GA_MEASUREMENT_ID}"></script>
    <script>
      window.dataLayer = window.dataLayer || [];
      function gtag(){{dataLayer.push(arguments);}}
      gtag('js', new Date());
      gtag('config', '{GA_MEASUREMENT_ID}');
    </script>
    """, height=1)
    
# App Configuration
st.set_page_config(layout="wide")
st.title("🌿 NDVI Explorer")
st.markdown("Upload a GeoJSON file or draw an AOI, then choose a date range to compute NDVI from Sentinel-2 imagery.")

# Sidebar Inputs
with st.sidebar:
    st.header("Input Parameters")
    uploaded_file = st.file_uploader("Upload AOI (GeoJSON)", type=["geojson", "json"])
    start_date = st.date_input("Start Date", value=datetime(2024, 1, 1))
    end_date = st.date_input("End Date", value=datetime(2024, 12, 31))
    cloud_cover = st.slider("Max Cloud Cover (%)", 0, 100, 10)
    run_button = st.button("Run Analysis")

# Map for AOI Selection
m = folium.Map(location=[-1.3, 36.8], zoom_start=12, tiles="CartoDB positron")
draw = plugins.Draw(export=True)
m.add_child(draw)
st.markdown("### Draw or Upload AOI")
output = st_folium(m, width=700, height=500)

# Fetch items
def fetch_items(bounds, date_range, cloud_limit):
    client = pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1")
    search = client.search(
        collections=["sentinel-2-l2a"],
        bbox=bounds,
        datetime=date_range,
        query={"eo:cloud_cover": {"lt": cloud_limit}}
    )
    items = list(search.get_items())
    
    if len(items) > 0:
        st.write("📦 Available images:", len(items))
    return items

# Best items
def filter_best_items(items):
    gdf_items = gpd.GeoDataFrame([
        {"id": item.id, "cloud": item.properties["eo:cloud_cover"], "geometry": shape(item.geometry)}
        for item in items
    ], geometry="geometry", crs="EPSG:4326")
    gdf_items["hash"] = gdf_items.geometry.apply(lambda g: hash(g.wkb_hex))
    best = gdf_items.loc[gdf_items.groupby("hash")["cloud"].idxmin()]
    return [item for item in items if item.id in best.id.tolist()]

def compute_ndvi_workflow(items, original_gdf):
    try:
        # Sign items for Planetary Computer
        import planetary_computer
        items = [planetary_computer.sign(item) for item in items]

        # Rebuild gdf
        gdf = gpd.GeoDataFrame(geometry=original_gdf.geometry)
        bounds = gdf.total_bounds.tolist()

        # Stack
        stack = stackstac.stack(
            items,
            assets=["B08", "B04"],
            resolution=10,
            epsg=6933,
            dtype="float",
            bounds_latlon=bounds
        )

        # Reproject geometry to match stack CRS
        gdf_proj = gdf.to_crs(stack.rio.crs)
        aoi_geom = gdf_proj.geometry.unary_union

        # Clip
        clipped = stack.rio.clip([aoi_geom], crs=stack.rio.crs)

        # NDVI
        nir = clipped.sel(band="B08").astype(float)
        red = clipped.sel(band="B04").astype(float)
        ndvi = (nir - red) / (nir + red)

        # Composite
        composite = ndvi.max(dim="time")

        with ProgressBar():
            lowres = composite.coarsen(x=4, y=4, boundary='pad').mean()
            ndvi_img = lowres.compute()
            ndvi_data = composite.compute()

        if np.isnan(ndvi_data.values).all():
            return None

        stats = {
            "mean": float(ndvi_data.mean().values),
            "min": float(ndvi_data.min().values),
            "max": float(ndvi_data.max().values)
        }

        return {
            "ndvi_data": ndvi_data,
            "ndvi_img": ndvi_img,
            "statistics": stats
        }

    except Exception as e:
        st.error(f"NDVI computation error: {e}")
        return None

# Main app logic
if run_button:
    try:
        # Load AOI
        if uploaded_file:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.geojson') as tmp:
                tmp.write(uploaded_file.read())
                gdf = gpd.read_file(tmp.name)
                os.unlink(tmp.name)
        elif output.get("all_drawings"):
            geojson = output["all_drawings"][0]
            gdf = gpd.GeoDataFrame.from_features([geojson], crs="EPSG:4326")
        else:
            st.error("Please upload or draw an AOI.")
            st.stop()

        # Display geometry info before fetch
        st.write("📍 AOI Geometry (WKT):")
        st.text(gdf.geometry.iloc[0].wkt)

        st.write("🧭 Bounding Box for STAC Search:")
        st.json(dict(zip(["minx", "miny", "maxx", "maxy"], gdf.total_bounds.tolist())))

        # Area validation
        area_km2 = gdf.to_crs("EPSG:6933").area[0] / 1e6
        if area_km2 > MAX_AREA_KM2:
            st.error(f"AOI exceeds max size of {MAX_AREA_KM2} km². Current: {area_km2:.2f} km²")
            st.stop()

        st.success(f"AOI loaded: {area_km2:.2f} km²")

        # Fetch imagery
        bounds = gdf.total_bounds.tolist()
        date_range = f"{start_date}/{end_date}"
        items = fetch_items(bounds, date_range, cloud_cover)
        if len(items) == 0:
            st.error("No scenes found.")
            st.stop()

        best_items = filter_best_items(items)
        st.info(f"{len(best_items)} best scene(s) selected.")

        # Compute NDVI
        result = compute_ndvi_workflow(best_items, gdf)

        if result is None:
            st.error("No valid NDVI data found.")
            st.stop()

        # Display stats
        st.subheader("NDVI Statistics")
        st.json(result["statistics"])

        # Plot
        st.subheader("NDVI Plot")
        fig = px.imshow(
            result["ndvi_data"].values,
            color_continuous_scale="YlGn",
            origin="upper",
            title="NDVI Composite"
        )
        fig.update_layout(height=500, width=800)
        st.plotly_chart(fig, use_container_width=True)

    except Exception as e:
        st.error("An error occurred during analysis.")
        st.exception(e)

st.markdown("---")
st.markdown("Built using Streamlit, Planetary Computer & STAC")
