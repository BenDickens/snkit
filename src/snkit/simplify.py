"""Network representation and utilities
"""
import os

import numpy as np
import shapely as sp
import pandas as pd
import shapely.errors
import pygeos
import pygeos.geometry as pygeom
import pyproj
from timeit import default_timer as timer
import igraph as ig
from geopandas import GeoDataFrame
from shapely.geometry import Point, MultiPoint, LineString, GeometryCollection, shape, mapping
from shapely.ops import split, linemerge
from tqdm import tqdm
#from pgpkg import Geopackage

# optional progress bars
'''
if 'SNKIT_PROGRESS' in os.environ and os.environ['SNKIT_PROGRESS'] in ('1', 'TRUE'):
    try:
        from tqdm import tqdm
    except ImportError:
        from snkit.utils import tqdm_standin as tqdm
else:
    from snkit.utils import tqdm_standin as tqdm
'''

class Network():
    """A Network is composed of nodes (points in space) and edges (lines)

    Parameters
    ----------
    nodes : geopandas.geodataframe.GeoDataFrame, optional
    edges : geopandas.geodataframe.GeoDataFrame, optional

    Attributes
    ----------
    nodes : geopandas.geodataframe.GeoDataFrame
    edges : geopandas.geodataframe.GeoDataFrame

    """
    def __init__(self, nodes=None, edges=None):
        """
        """
        if nodes is None:
            nodes = pd.DataFrame()
        self.nodes = nodes

        if edges is None:
            edges = pd.DataFrame()
        self.edges = edges

    def set_crs(self, crs=None, epsg=None):
        """Set network (node and edge) crs

        Parameters
        ----------
        crs : dict or str
            Projection parameters as PROJ4 string or in dictionary form.
        epsg : int
            EPSG code specifying output projection

        """
        if crs is None and epsg is None:
            raise ValueError("Either crs or epsg must be provided to Network.set_crs")

        if epsg is not None:
            crs = {'init': 'epsg:{}'.format(epsg)}

        self.edges.crs = crs
        self.nodes.crs = crs

    def to_crs(self, crs=None, epsg=None):
        """Set network (node and edge) crs

        Parameters
        ----------
        crs : dict or str
            Projection parameters as PROJ4 string or in dictionary form.
        epsg : int
            EPSG code specifying output projection

        """
        if crs is None and epsg is None:
            raise ValueError("Either crs or epsg must be provided to Network.set_crs")

        if epsg is not None:
            crs = {'init': 'epsg:{}'.format(epsg)}

        self.edges.to_crs(crs, inplace=True)
        self.nodes.to_crs(crs, inplace=True)


def add_ids(network, id_col='id', edge_prefix='', node_prefix=''):
    """Add or replace an id column with ascending ids
    """
    nodes = network.nodes.copy()
    if not nodes.empty:
        nodes = nodes.reset_index(drop=True)

    edges = network.edges.copy()
    if not edges.empty:
        edges = edges.reset_index(drop=True)
    '''The ids have been changed to int64s for easier conversion to numpy arrays
    nodes[id_col] = ['{}{}'.format(node_prefix, i) for i in range(len(nodes))]
    edges[id_col] = ['{}{}'.format(edge_prefix, i) for i in range(len(edges))]
    '''
    nodes[id_col] = range(len(nodes))
    edges[id_col] = range(len(edges))

    return Network(
        nodes=nodes,
        edges=edges
    )


def add_topology(network, id_col='id'):
    """Add or replace from_id, to_id to edges
    """
    from_ids = []
    to_ids = []
    node_ends = []
    bugs = []
    sindex = pygeos.STRtree(network.nodes.geometry)
    for edge in tqdm(network.edges.itertuples(), desc="topology", total=len(network.edges)):
        start, end = line_endpoints(edge.geometry)

        try: 
            start_node = nearest_node(start, network.nodes,sindex)
            from_ids.append(start_node[id_col])
        except:
            bugs.append(edge.id)
            from_ids.append(-1)
        try:
            end_node = nearest_node(end, network.nodes,sindex)
            to_ids.append(end_node[id_col])
        except:
            bugs.append(edge.id)
            to_ids.append(-1)
    print(len(bugs)," Edges not connected to nodes")
    edges = network.edges.copy()
    nodes = network.nodes.copy()
    edges['from_id'] = from_ids
    edges['to_id'] = to_ids
    edges = edges.loc[~(edges.id.isin(list(bugs)))].reset_index(drop=True)

    return Network(
        nodes=network.nodes,
        edges=edges
    )


def get_endpoints(network):
    """Get nodes for each edge endpoint
    """
    endpoints = []
    for edge in tqdm(network.edges.itertuples(), desc="endpoints", total=len(network.edges)):
        if edge.geometry is None:
            continue
        # 5 is MULTILINESTRING
        if pygeom.get_type_id(edge.geometry) == '5':
            for line in edge.geometry.geoms:
                start, end = line_endpoints(line)
                endpoints.append(start)
                endpoints.append(end)
        else:
            start, end = line_endpoints(edge.geometry)
            endpoints.append(start)
            endpoints.append(end)

    # create dataframe to match the nodes geometry column name
    return matching_gdf_from_geoms(network.nodes, endpoints)


def add_endpoints(network):
    """Add nodes at line endpoints
    """
    endpoints = get_endpoints(network)
    nodes = concat_dedup([network.nodes, endpoints])

    return Network(
        nodes=nodes,
        edges=network.edges
    )


def round_geometries(network, precision=3):
    """Round coordinates of all node points and vertices of edge linestrings to some precision
    """
    def _set_precision(geom):
        return set_precision(geom, precision)
    network.nodes.geometry = network.nodes.geometry.apply(_set_precision)
    network.edges.geometry = network.edges.geometry.apply(_set_precision)
    return network


def split_multilinestrings(network):
    """Create multiple edges from any MultiLineString edge

    Ensures that edge geometries are all LineStrings, duplicates attributes over any
    created multi-edges.
    """
    simple_edge_attrs = []
    simple_edge_geoms = []
    edges = network.edges
    for edge in tqdm(edges.itertuples(index=False), desc="split_multi", total=len(edges)):
        if edge.geometry.geom_type == 'MultiLineString':
            edge_parts = list(edge.geometry)
        else:
            edge_parts = [edge.geometry]

        for part in edge_parts:
            simple_edge_geoms.append(part)

        attrs = GeoDataFrame([edge] * len(edge_parts))
        simple_edge_attrs.append(attrs)

    simple_edge_geoms = GeoDataFrame(simple_edge_geoms, columns=['geometry'])
    edges = pd.concat(simple_edge_attrs, axis=0).reset_index(drop=True).drop('geometry', axis=1)
    edges = pd.concat([edges, simple_edge_geoms], axis=1)

    return Network(
        nodes=network.nodes,
        edges=edges
    )

#Written for comparison of geometry's not mergeable - the pygeos with conversion is almost as fast
#as shapely!
#Mainly kept in to remind us to move to pygeos once integrated with GeoPandas
def line_merge(x):
    if pygeom.get_type_id(x) == '5':
        return pygeos.linear.line_merge(x)
    else: return x

#Same as above
def merge_all_multi(network):
    edges = network.edges.copy()
    edges['geometry']= edges.geometry.apply(lambda x: line_merge(x))
    return Network(edges=edges,nodes=network.nodes)

def merge_multilinestring(geom):
    """ Merge a MultiLineString to LineString
    """
    try:
        if geom.geom_type == 'MultiLineString':
            geom_inb = linemerge(geom)
            if geom_inb.is_ring:
                return geom_inb
# In case of linestring merge issues, we could add this to the script again
#            from centerline.main import Centerline
#            if geom_inb.geom_type == 'MultiLineString':
#                return linemerge(Centerline(geom.buffer(0.5)))
            else:
                return geom_inb
        else:
            return geom
    except:
        return GeometryCollection()

def snap_nodes(network, threshold=None):
    """Move nodes (within threshold) to edges
    """
    def snap_node(node):
        snap = nearest_point_on_edges(node.geometry, network.edges)
        distance = snap.distance(node.geometry)
        if threshold is not None and distance > threshold:
            snap = node.geometry
        return snap

    snapped_geoms = network.nodes.apply(snap_node, axis=1)
    geom_col = geometry_column_name(network.nodes)
    nodes = pd.concat([
        network.nodes.drop(geom_col, axis=1),
        GeoDataFrame(snapped_geoms, columns=[geom_col])
    ], axis=1)

    return Network(
        nodes=nodes,
        edges=network.edges
    )


def split_edges_at_nodes(network, tolerance=1e-9):
    """Split network edges where they intersect node geometries
    """
    split_edges = []
    for edge in tqdm(network.edges.itertuples(index=False), desc="split", total=len(network.edges)):
        hits = nodes_intersecting(edge.geometry, network.nodes, tolerance)
        split_points = MultiPoint([hit.geometry for hit in hits.itertuples()])

        # potentially split to multiple edges
        edges = split_edge_at_points(edge, split_points, tolerance)
        split_edges.append(edges)

    # combine dfs
    edges = pd.concat(split_edges, axis=0)
    # reset index and drop
    edges = edges.reset_index().drop('index', axis=1)
    # return new network with split edges
    return Network(
        nodes=network.nodes,
        edges=edges
    )


def link_nodes_to_edges_within(network, distance, condition=None, tolerance=1e-9):
    """Link nodes to all edges within some distance
    """
    new_node_geoms = []
    new_edge_geoms = []
    for node in tqdm(network.nodes.itertuples(index=False), desc="link", total=len(network.nodes)):
        # for each node, find edges within
        edges = edges_within(node.geometry, network.edges, distance)
        for edge in edges.itertuples():
            if condition is not None and not condition(node, edge):
                continue
            # add nodes at points-nearest
            point = nearest_point_on_line(node.geometry, edge.geometry)
            if point != node.geometry:
                new_node_geoms.append(point)
                # add edges linking
                line = LineString([node.geometry, point])
                new_edge_geoms.append(line)

    new_nodes = matching_gdf_from_geoms(network.nodes, new_node_geoms)
    all_nodes = concat_dedup([network.nodes, new_nodes])

    new_edges = matching_gdf_from_geoms(network.edges, new_edge_geoms)
    all_edges = concat_dedup([network.edges, new_edges])

    # split edges as necessary after new node creation
    unsplit = Network(
        nodes=all_nodes,
        edges=all_edges
    )
    return split_edges_at_nodes(unsplit, tolerance)


def link_nodes_to_nearest_edge(network, condition=None):
    """Link nodes to all edges within some distance
    """
    new_node_geoms = []
    new_edge_geoms = []
    for node in tqdm(network.nodes.itertuples(index=False), desc="link", total=len(network.nodes)):
        # for each node, find edges within
        edge = nearest_edge(node.geometry, network.edges)
        if condition is not None and not condition(node, edge):
            continue
        # add nodes at points-nearest
        point = nearest_point_on_line(node.geometry, edge.geometry)
        if point != node.geometry:
            new_node_geoms.append(point)
            # add edges linking
            line = LineString([node.geometry, point])
            new_edge_geoms.append(line)

    new_nodes = matching_gdf_from_geoms(network.nodes, new_node_geoms)
    all_nodes = concat_dedup([network.nodes, new_nodes])

    new_edges = matching_gdf_from_geoms(network.edges, new_edge_geoms)
    all_edges = concat_dedup([network.edges, new_edges])

    # split edges as necessary after new node creation
    unsplit = Network(
        nodes=all_nodes,
        edges=all_edges
    )
    return split_edges_at_nodes(unsplit)

#Methods to clean roundabouts and junctions should be done before
#splitting edges at nodes to avoid logic conflicts
def find_roundabouts(network):
    roundabouts = []
    for edge in network.edges.itertuples():
        if pygeos.predicates.is_ring(edge.geometry): roundabouts.append(edge)
    return roundabouts


def clean_roundabouts(network):
    sindex = pygeos.STRtree(network.edges['geometry'])
    edges = network.edges
    new_geom = network.edges
    new_edge = []
    remove_edge=[]
    new_edge_id = []


    roundabouts = find_roundabouts(network)
    testy = []
    
    for roundabout in roundabouts:

        round_bound = pygeos.constructive.boundary(roundabout.geometry)
        round_centroid = pygeos.constructive.centroid(roundabout.geometry)
        remove_edge.append(roundabout.Index)

        edges_intersect = _intersects_pyg(roundabout.geometry, network.edges['geometry'], sindex)
        #Drop the roundabout from series so that no snapping happens on it
        edges_intersect.drop(roundabout.Index,inplace=True)
        #index at e[0] geometry at e[1] of edges that intersect with 
        for e in edges_intersect.items():
            edge = edges.iloc[e[0]]
            start = pygeom.get_point(e[1],0)
            end = pygeom.get_point(e[1],-1)
            first_co_is_closer = pygeos.measurement.distance(end, round_centroid) > pygeos.measurement.distance(start, round_centroid) 
            #print(first_co_is_closer)
            #print("start ",start," and end ", end)
            co_ords = pygeos.coordinates.get_coordinates(edge.geometry)
            centroid_co = pygeos.coordinates.get_coordinates(round_centroid)
            #print(type(co_ords))
            #print(type(centroid_co))
            if first_co_is_closer: 
                new_co = np.concatenate((centroid_co,co_ords))
            else:
                new_co = np.concatenate((co_ords,centroid_co))
            snap_line = pygeos.linestrings(new_co)

            snap_line = pygeos.linestrings(new_co)
            #an edge should never connect to more than 2 roundabouts, if it does this will break
            if edge.osm_id in new_edge_id:
                a = []
                counter = 0
                for x in new_edge:
                    if x[0]==edge.osm_id:
                        a = counter
                        break
                    counter += 1
                double_edge = new_edge.pop(a)
                start = pygeom.get_point(double_edge[2],0)
                end = pygeom.get_point(double_edge[2],-1)
                first_co_is_closer = pygeos.measurement.distance(end, round_centroid) > pygeos.measurement.distance(start, round_centroid) 
                co_ords = pygeos.coordinates.get_coordinates(double_edge[2])
                if first_co_is_closer: 
                    new_co = np.concatenate((centroid_co,co_ords))
                else:
                    new_co = np.concatenate((co_ords,centroid_co))
                snap_line = pygeos.linestrings(new_co)
                new_edge.append([edge.osm_id, edge.highway, snap_line])

            else:
                new_edge.append([edge.osm_id, edge.highway, snap_line])
                new_edge_id.append(edge.osm_id)
            remove_edge.append(e[0])

    new = pd.DataFrame(new_edge,columns=['osm_id','highway','geometry'])
    dg = network.edges.loc[~network.edges.index.isin(remove_edge)]
    
    ges = pd.concat([dg,new]).reset_index()

    return Network(edges=ges, nodes=network.nodes)

#Simply returns a dataframe of nodes with degree 1, technically not all of 
#these are "hanging"
def find_hanging_nodes(network):
    hang_index = np.where(network.nodes['degree']==1)
    return network.nodes.iloc[hang_index]

#This method adds a distance column using pygeos (converted from shapely)
#assuming the new crs from the latitude and longitude of the first node
#distance is in metres
def add_distances(network):
    #Find crs of current gdf and arbitrary point(lat,lon) for new crs
    current_crs="epsg:4326"
    #The commented out crs does not work in all cases
    #current_crs = [*network.edges.crs.values()]
    #current_crs = str(current_crs[0])
    lat = pygeom.get_y(network.nodes['geometry'].iloc[0])
    lon = pygeom.get_x(network.nodes['geometry'].iloc[0])
    # formula below based on :https://gis.stackexchange.com/a/190209/80697 
    approximate_crs = "epsg:" + str(int(32700-np.round((45+lat)/90,0)*100+np.round((183+lon)/6,0)))
    #from pygeos/issues/95
    geometries = network.edges['geometry']
    coords = pygeos.get_coordinates(geometries)
    transformer=pyproj.Transformer.from_crs(current_crs, approximate_crs,always_xy=True)
    new_coords = transformer.transform(coords[:, 0], coords[:, 1])
    result = pygeos.set_coordinates(geometries.copy(), np.array(new_coords).T)
    dist = pygeos.length(result)
    edges = network.edges.copy()
    edges['distance'] = dist
    return Network(
        nodes=network.nodes,
        edges=edges)

#Time is in hours
def add_travel_time(network):
    if 'distance' not in network.nodes.columns:
        network = add_distances(network)
    speed_d = {
    'motorway':80000,
    'motorway_link': 65000,
    'trunk': 60000,
    'trunk_link':50000,
    'primary': 50000, # metres ph
    'primary_link':40000,
    'secondary': 40000, # metres ph
    'secondary_link':30000,
    'tertiary':30000,
    'tertiary_link': 20000,
    'unclassified':20000,
    'residential': 20000,  # mph
    }
    def calculate_time(edge):
        return edge['distance'] / speed_d.get(edge['highway'])

    network.edges['time'] = network.edges.apply(calculate_time,axis=1)
    return network


#Calculates the degree of the nodes from the from and to ids
#It is not wise to call this method after removing nodes or edges 
#without first resetting the ids
def calculate_degree(network):
    #the number of nodes(from index) to use as the number of bins
    ndC = len(network.nodes.index)
    if ndC-1 > max(network.edges.from_id) and ndC-1 > max(network.edges.to_id): print("Calculate_degree possibly unhappy")
    return np.bincount(network.edges['from_id'],None,ndC) + np.bincount(network.edges['to_id'],None,ndC)

#Adds a degree column to the node geodataframe 
def add_degree(network):
    degree = calculate_degree(network)
    network.nodes['degree'] = degree

#This method drops any single degree nodes and their associated edges 
#given a distance(degrees) threshold.  This primarily happens when 
#a road was connected to residential areas, most often these are link
#roads that no longer do so
def drop_hanging_nodes(network, tolerance = 0.005):
    if 'degree' not in network.nodes.columns:
        deg = calculate_degree(network)
    else: deg = network.nodes['degree'].to_numpy()
    #hangNodes : An array of the indices of nodes with degree 1
    hangNodes = np.where(deg==1)
    ed = network.edges.copy()
    to_ids = ed['to_id'].to_numpy()
    from_ids = ed['from_id'].to_numpy()
    hangTo = np.isin(to_ids,hangNodes)
    hangFrom = np.isin(from_ids,hangNodes)
    #eInd : An array containing the indices of edges that connect
    #the degree 1 nodes
    eInd = np.hstack((np.nonzero(hangTo),np.nonzero(hangFrom)))
    degEd = ed.iloc[np.sort(eInd[0])]
    edge_id_drop = []
    for d in degEd.itertuples():
        dist = pygeos.measurement.length(d.geometry)
        #If the edge is shorter than the tolerance
        #add the ID to the drop list and update involved node degrees
        if dist < tolerance:
            edge_id_drop.append(d.id)
            deg[d.from_id] -= 1
            deg[d.to_id] -= 1
        # drops disconnected edges, some may still persist since we have not merged yet
        if deg[d.from_id] == 1 and deg[d.to_id] == 1: 
            edge_id_drop.append(d.id)
            deg[d.from_id] -= 1
            deg[d.to_id] -= 1
    
    edg = ed.loc[~(ed.id.isin(edge_id_drop))].reset_index(drop=True)
    aa = ed.loc[ed.id.isin(edge_id_drop)]
    edg.drop(labels=['id'],axis=1,inplace=True)
    edg['id'] = range(len(edg))
    n = network.nodes.copy()
    n['degree'] = deg
    #Degree 0 Nodes are cleaned in the merge_2 method
    #x = n.loc[n.degree==0]
    #nod = n.loc[n.degree > 0].reset_index(drop=True)
    return Network(nodes = n,edges=edg)


#This method removes all degree 2 nodes and merges their associated edges, 
#at the moment it arbitrarily uses the first edge's attributes for the 
#new edges column attributes, in the future the mean or another measure 
#can be used to set these new values.
#The general strategy is to find a node of degree 2, and the associated 
#2 edges, then traverse edges and nodes in both directions until a node
#of degree !=2 is found, at this point stop in this direction. Reset the 
#geometry and from/to ids for this edge, delete the nodes and edges traversed. 
def merge_2(network, print_err=False):
    net = network
    nod = net.nodes.copy()
    edg = net.edges.copy()
    edg_sindex = pygeos.STRtree(network.edges.geometry)
    if 'degree' not in network.nodes.columns:
        deg = calculate_degree(network)
    else: deg = nod['degree'].to_numpy()
    #For the 0.002s speed up, alternatively do a straightforward loc[degree==2]
    degree2 = np.where(deg==2)
    #n2: is the set of all node IDs that are degree 2
    n2 = set((nod['id'].iloc[degree2]))
    #TODO if you create a dictionary to mask values this geometry
    #array nodGeom can be made to only contain the 'geometry' of degree 2
    #nodes
    nodGeom = nod['geometry']
    eIDtoRemove =[]
    nIDtoRemove =[]

    c = 0
    #pbar = tqdm(total=len(n2))
    while n2:   
        newEdge = []
        info_first_edge = []
        possibly_delete = []
        pos_0_deg = []
        nodeID = n2.pop()
        pos_0_deg.append(nodeID)
        #deg[nodeID]= 0
        #Co-ordinates of current node
        node_geometry = nodGeom[nodeID]
        eID = set(edg_sindex.query(nodGeom[nodeID],predicate='intersects'))
        #Find the nearest 2 edges, unless there is an error in the dataframe
        #this will return the connected edges using spatial indexing
        if len(eID) > 2: edgePath1, edgePath2 = find_closest_2_edges(eID,nodeID,edg,node_geometry)
        elif len(eID) < 2: 
            #print("First set only contains ", len(eID), "edges")#
            continue
        else: 
            edgePath1 = edg.iloc[eID.pop()]
            edgePath2 = edg.iloc[eID.pop()] 
        #For the two edges found, identify the next 2 nodes in either direction    
        nextNode1 = edgePath1.to_id if edgePath1.from_id==nodeID else edgePath1.from_id
        nextNode2 = edgePath2.to_id if edgePath2.from_id==nodeID else edgePath2.from_id
        if nextNode1==nextNode2: continue
        possibly_delete.append(edgePath2.id)
        #At the moment the first edge information is used for the merged edge
        info_first_edge = edgePath1.id
        newEdge.append(edgePath1.geometry)
        newEdge.append(edgePath2.geometry)
        #While the next node along the path is degree 2 keep traversing
        while deg[nextNode1] == 2:
            if nextNode1 in pos_0_deg: break
            eID = set(edg_sindex.query(nodGeom[nextNode1],predicate='intersects'))
            eID.discard(edgePath1.id)
            try:
                edgePath1 = min([edg.iloc[match_idx] for match_idx in eID],
                key= lambda match: pygeos.distance(nodGeom[nextNode2],(match.geometry)))
            except: 
                continue
            pos_0_deg.append(nextNode1)
            #deg[nextNode1] = 0
            n2.discard(nextNode1)
            nextNode1 = edgePath1.to_id if edgePath1.from_id==nextNode1 else edgePath1.from_id
            newEdge.append(edgePath1.geometry)
            possibly_delete.append(edgePath1.id)

        while deg[nextNode2] == 2:
            if nextNode2 in pos_0_deg: break
            eID = set(edg_sindex.query(nodGeom[nextNode2],predicate='intersects'))
            eID.discard(edgePath2.id)
            try:
                edgePath2 = min([edg.iloc[match_idx] for match_idx in eID],
                key= lambda match: pygeos.distance(nodGeom[nextNode2],(match.geometry)))
            except: continue
            pos_0_deg.append(nextNode2)
            #deg[nextNode2] = 0
            n2.discard(nextNode2)
            nextNode2 = edgePath2.to_id if edgePath2.from_id==nextNode2 else edgePath2.from_id
            newEdge.append(edgePath2.geometry)
            possibly_delete.append(edgePath2.id)
        #Update the information of the first edge
        new_merged_geom = pygeos.line_merge(pygeos.multilinestrings([x for x in newEdge]))
        if pygeom.get_type_id(new_merged_geom) == 1: 
            edg.at[info_first_edge,'geometry'] = new_merged_geom
            edg.at[info_first_edge,'from_id'] = nextNode1
            edg.at[info_first_edge,'to_id'] = nextNode2
            eIDtoRemove += possibly_delete
            for x in pos_0_deg:
                deg[x] = 0
        else:
            if print_err: print("Line", info_first_edge, "failed to merge, has pygeos type ", pygeom.get_type_id(edg.at[info_first_edge,'geometry']))

            


        #pbar.update(1)
    
    #pbar.close()
    edg = edg.loc[~(edg.id.isin(eIDtoRemove))].reset_index(drop=True)
    #We remove all degree 0 nodes, including those found in dropHanging
    n = nod.loc[nod.degree > 0].reset_index(drop=True)
    #n=nod.reset_index(drop=True)
    return Network(nodes=n,edges=edg)


#IReturns the 2 edges connected to the current node
def find_closest_2_edges(edgeIDs, nodeID, edges, nodGeometry):
    edgePath1 = min([edges.iloc[match_idx] for match_idx in edgeIDs],
            key=lambda match: pygeos.distance(nodGeometry,match.geometry))
    edgeIDs.remove(edgePath1.id)
    edgePath2 = min([edges.iloc[match_idx] for match_idx in edgeIDs],
            key=lambda match:  pygeos.distance(nodGeometry,match.geometry))
    return edgePath1, edgePath2


def merge_edges(network):
    """ Merge edges that share a node with a connectivity degree of 2
    """
    if 'degree' not in network.nodes.columns:
        network.nodes['degree'] = network.nodes.id.apply(lambda x:
                                                 node_connectivity_degree(x,network))

    degree2 = list(network.nodes.id.loc[network.nodes.degree == 2])
    d2_set = set(degree2)
    node_paths = []
    edge_paths = []
    print(d2_set)
    while d2_set:
        popped_node = d2_set.pop()
        node_path = [popped_node]
        candidates = set([popped_node])
        print(candidates)
        while candidates:
            popped_cand = candidates.pop()
            matches = list(np.unique(network.edges[['from_id','to_id']].loc[(
                    (network.edges.from_id.isin([popped_cand])) |
                    (network.edges.to_id.isin([popped_cand])))].values))
            matches.remove(popped_cand)
            for match in matches:
                if match in node_path:
                    continue

                if match in degree2:
                    candidates.add(match)
                    node_path.append(match)
                    d2_set.remove(match)
                else:
                    node_path.append(match)
        if len(node_path) > 2:
            node_paths.append(node_path)
            edge_paths.append(network.edges.loc[(
                    (network.edges.from_id.isin(node_path)) &
                    (network.edges.to_id.isin(node_path)))])

    concat_edge_paths = []
    unique_edge_ids = set()
    for edge_path in edge_paths:
        unique_edge_ids.update(list(edge_path.id))
        #if edge_path.bridge.isnull().any():
         #   edge_path = edge_path.copy()
          #  edge_path['bridge'] = 'yes'
        concat_edge_paths.append(edge_path.dissolve(by=['infra_type'], aggfunc='first'))

    edges_new = network.edges.copy()
    edges_new = edges_new.loc[~(edges_new.id.isin(list(unique_edge_ids)))]
    edges_new.geometry = edges_new.geometry.apply(merge_multilinestring)
    network.edges = pd.concat([edges_new,pd.concat(concat_edge_paths).reset_index()],sort=False)

    nodes_new = network.nodes.copy()
    network.nodes = nodes_new.loc[~(nodes_new.id.isin(list(degree2)))]

    return Network(
        nodes=network.nodes,
        edges=network.edges
    )


def geometry_column_name(gdf):
    """Get geometry column name, fall back to 'geometry'
    """
    try:
        geom_col = gdf.geometry.name
    except AttributeError:
        geom_col = 'geometry'
    return geom_col


def matching_gdf_from_geoms(gdf, geoms):
    """Create a geometry-only GeoDataFrame with column name to match an existing GeoDataFrame
    """
    geom_col = geometry_column_name(gdf)
    return pd.DataFrame(geoms, columns=[geom_col])

def concat_dedup(dfs):
    """Concatenate a list of GeoDataFrames, dropping duplicate geometries
    - note: repeatedly drops indexes for deduplication to work
    """
    cat = pd.concat(dfs, axis=0, sort=False)
    cat.reset_index(drop=True, inplace=True)
    cat_dedup = drop_duplicate_geometries(cat)
    cat_dedup.reset_index(drop=True, inplace=True)
    return cat_dedup

def node_connectivity_degree(node, network):
    return len(
            network.edges[
                (network.edges.from_id == node) | (network.edges.to_id == node)
            ]
    )

def drop_duplicate_geometries(gdf, keep='first'):
    """Drop duplicate geometries from a dataframe
    """
    # convert to wkb so drop_duplicates will work
    # discussed in https://github.com/geopandas/geopandas/issues/521
    mask = gdf.geometry.apply(lambda geom: pygeos.to_wkb(geom))
    # use dropped duplicates index to drop from actual dataframe
    return gdf.iloc[mask.drop_duplicates(keep).index]

def nearest_point_on_edges(point, edges):
    """Find nearest point on edges to a point
    """
    edge = nearest_edge(point, edges)
    snap = nearest_point_on_line(point, edge.geometry)
    return snap

def nearest_node(point, nodes,sindex):
    """Find nearest node to a point
    """
    return nearest(point, nodes,sindex)

def nearest_edge(point, edges,sindex):
    """Find nearest edge to a point
    """
    return nearest(point, edges,sindex)

def nearest(geom, gdf,sindex):
    """Find the element of a GeoDataFrame nearest a shapely geometry
    """
    #sindex = pygeos.STRtree(gdf['geometry'])
    matches_idx = sindex.query(geom)
    #pygeos.measurement.bounds(geom)
    #matches_idx = gdf.sindex.nearest(geom.bounds)
    nearest_geom = min(
        [gdf.iloc[match_idx] for match_idx in matches_idx],
        key=lambda match: pygeos.measurement.distance(match.geometry,geom)
    )
    return nearest_geom

def edges_within(point, edges, distance):
    """Find edges within a distance of point
    """
    return d_within(point, edges, distance)

def nodes_intersecting(line, nodes, tolerance=1e-9):
    """Find nodes intersecting line
    """
    return intersects(line, nodes, tolerance)

def intersects(geom, gdf, tolerance=1e-9):
    """Find the subset of a GeoDataFrame intersecting with a shapely geometry
    """
    return _intersects(geom, gdf, tolerance)

def d_within(geom, gdf, distance):
    """Find the subset of a GeoDataFrame within some distance of a shapely geometry
    """
    return _intersects(geom, gdf, distance)

def _intersects(geom, gdf, tolerance=1e-9):
    buf = geom.buffer(tolerance)
    if buf.is_empty:
        # can have an empty buffer with too small a tolerance, fallback to original geom
        buf = geom
    try:
        return _intersects_gdf(buf, gdf)
    except shapely.errors.TopologicalError:
        # can exceptionally buffer to an invalid geometry, so try re-buffering
        buf = buf.buffer(0)
        return _intersects_gdf(buf, gdf)

def _intersects_gdf(geom, gdf):
    candidate_idxs = list(gdf.sindex.intersection(geom.bounds))
    candidates = gdf.iloc[candidate_idxs]
    return candidates[candidates.intersects(geom)]

def line_endpoints(line):
    """Return points at first and last vertex of a line
    """
    start = pygeom.get_point(line,0)
    end = pygeom.get_point(line,-1)
    #start = Point(line.coords[0])
    #end = Point(line.coords[-1])
    return start, end


def split_edge_at_points(edge, points, tolerance=1e-9):
    """Split edge at point/multipoint
    """
    try:
        segments = split_line(edge.geometry, points, tolerance)
    except ValueError:
        # if splitting fails, e.g. becuase points is empty GeometryCollection
        segments = [edge.geometry]
    edges = GeoDataFrame([edge] * len(segments))
    edges.geometry = segments
    return edges

def split_line(line, points, tolerance=1e-9):
    """Split line at point or multipoint, within some tolerance
    """
    to_split = snap_line(line, points, tolerance)
    return list(split(to_split, points))

def snap_line(line, points, tolerance=1e-9):
    """Snap a line to points within tolerance, inserting vertices as necessary
    """
    if points.geom_type == 'Point':
        if points.distance(line) < tolerance:
            line = add_vertex(line, points)
    elif points.geom_type == 'MultiPoint':
        points = [point for point in points if point.distance(line) < tolerance]
        for point in points:
            line = add_vertex(line, point)
    return line

def add_vertex(line, point):
    """Add a vertex to a line at a point
    """
    v_idx = nearest_vertex_idx_on_line(point, line)
    point_coords = tuple(point.coords[0])

    if point_coords == line.coords[v_idx]:
        # nearest vertex could be identical to point, so return unchanged
        return line

    insert_before_idx = None
    if v_idx == 0:
        # nearest vertex could be start, so insert just after (or could extend)
        insert_before_idx = 1
    elif v_idx == len(line.coords) - 1:
        # nearest vertex could be end, so insert just before (or could extend)
        insert_before_idx = v_idx
    else:
        # otherwise insert in between vertices of nearest segment
        segment_before = LineString([line.coords[v_idx], line.coords[v_idx - 1]])
        segment_after = LineString([line.coords[v_idx], line.coords[v_idx + 1]])
        if point.distance(segment_before) < point.distance(segment_after):
            insert_before_idx = v_idx
        else:
            insert_before_idx = v_idx + 1
    # insert point coords before index, return new linestring
    new_coords = list(line.coords)
    new_coords.insert(insert_before_idx, point_coords)
    return LineString(new_coords)

def nearest_vertex_idx_on_line(point, line):
    """Return the index of nearest vertex to a point on a line
    """
    # distance to all points is calculated here - and this is called once per splitting point
    # any way to avoid this m x n behaviour?
    nearest_idx, _ = min(
        [(idx, point.distance(Point(coords))) for idx, coords in enumerate(line.coords)],
        key=lambda item: item[1]
    )
    return nearest_idx

def nearest_point_on_line(point, line):
    """Return the nearest point on a line
    """
    return line.interpolate(line.project(point))

def set_precision(geom, precision):
    """Set geometry precision
    """
    geom_mapping = mapping(geom)
    geom_mapping['coordinates'] = np.round(np.array(geom_mapping['coordinates']), precision)
    return shape(geom_mapping)

#Resets the ids of the nodes and edges, editing the refereces in edge table 
#using dict masking
def reset_ids(network):
    nodes = network.nodes.copy()
    edges = network.edges.copy()
    to_ids =  edges['to_id'].to_numpy()
    from_ids = edges['from_id'].to_numpy()
    new_node_ids = range(len(nodes))
    #creates a dictionary of the node ids and the actual indices
    id_dict = dict(zip(nodes.id,new_node_ids))
    nt = np.copy(to_ids)
    nf = np.copy(from_ids) 
    #updates all from and to ids, because many nodes are effected, this
    #is quite optimal approach for large dataframes
    for k,v in id_dict.items():
        nt[to_ids==k] = v
        nf[from_ids==k] = v
    edges.drop(labels=['to_id','from_id'],axis=1,inplace=True)
    edges['from_id'] = nf
    edges['to_id'] = nt
    nodes.drop(labels=['id'],axis=1,inplace=True)
    nodes['id'] = new_node_ids
    edges['id'] = range(len(edges))
    edges.reset_index(drop=True,inplace=True)
    nodes.reset_index(drop=True,inplace=True)
    return Network(edges=edges,nodes=nodes)

def _intersects_pyg(geom, gdf, sindex,tolerance=1e-9):
    buf = pygeos.buffer(geom,tolerance)
    if pygeos.is_empty(buf):
        # can have an empty buffer with too small a tolerance, fallback to original geom
        buf = geom
    try:
        return _intersects_gdf_pyg(buf, gdf,sindex)
    except shapely.errors.TopologicalError:  #this still needs to be changed
        # can exceptionally buffer to an invalid geometry, so try re-buffering
        buf = pygeos.buffer(geom,0)
        return _intersects_gdf_pyg(buf, gdf,sindex)
    
def _intersects_gdf_pyg(geom, gdf,sindex):
    return gdf[sindex.query(geom,'intersects')]

def intersects_pyg(geom, gdf, sindex, tolerance=1e-9):
    """Find the subset of a GeoDataFrame intersecting with a shapely geometry
    """
    return _intersects_pyg(geom, gdf, sindex, tolerance)

def nodes_intersecting_pyg(line,nodes,sindex,tolerance=1e-9):
    """Find nodes intersecting line
    """
    return intersects_pyg(line, nodes,sindex, tolerance)

def split_edges_at_nodes_pyg(network, tolerance=1e-9):
    """Split network edges where they intersect node geometries
    """
    #sindex_nodes = pygeos.STRtree(network.nodes['geometry'])
    sindex_edges = pygeos.STRtree(network.edges['geometry'])
    
    grab_all_edges = []
    new_nodes = []
    for edge in tqdm(network.edges.itertuples(index=False), desc="split", total=len(network.edges)):
        #hits_nodes = nodes_intersecting_pyg(edge.geometry,network.nodes['geometry'],sindex_nodes, tolerance=1e-9)
        hits_edges = nodes_intersecting_pyg(edge.geometry,network.edges['geometry'],sindex_edges, tolerance=1e-9)
        hits_edges = pygeos.set_operations.intersection(edge.geometry,hits_edges)
        hits_edges = (hits_edges[~(pygeos.predicates.covers(hits_edges,edge.geometry))])
        hits_edges = pd.Series([pygeos.points(item) for sublist in [pygeos.get_coordinates(x) for x in hits_edges] for item in sublist],name='geometry')
        #new_nodes.extend(hits_edges)
        hits = [pygeos.points(x) for x in pygeos.coordinates.get_coordinates(
            pygeos.constructive.extract_unique_points(hits_edges.values))]#pygeos.multipoints(hits_edges.values)))]#pd.concat([hits_nodes,hits_edges]).values)))]
        
        
        hits = pd.DataFrame(hits,columns=['geometry'])    
        
        #if (len(hits_nodes) < 3):
         #   grab_all_edges.append([[edge.osm_id],[edge.geometry],[edge.highway]])
          #  continue

        # get points and geometry as list of coordinates
        split_points = pygeos.coordinates.get_coordinates(pygeos.snap(hits,edge.geometry,tolerance=1e-9))
        coor_geom = pygeos.coordinates.get_coordinates(edge.geometry)
 
        # potentially split to multiple edges
        split_locs = np.argwhere(np.isin(coor_geom, split_points).all(axis=1))[:,0]
        split_locs = list(zip(split_locs.tolist(), split_locs.tolist()[1:]))

        new_edges = [coor_geom[split_loc[0]:split_loc[1]+1] for split_loc in split_locs]

        grab_all_edges.append([[edge.osm_id]*len(new_edges),[pygeos.linestrings(edge) for edge in new_edges],[edge.highway]*len(new_edges)])

    # combine all new edges
    edges = pd.DataFrame([item for sublist in  [list(zip(x[0],x[1],x[2])) for x in grab_all_edges] for item in sublist],
                         columns=['osm_id','geometry','highway'])
    #print(new_nodes)
    #new_nodes = matching_gdf_from_geoms(network.nodes, new_nodes)
    #nodes = concat_dedup([network.nodes, new_nodes])
    # return new network with split edges
    return Network(
        nodes=network.nodes,
        edges=edges
    )

#returns a geopandas dataframe of a simplified network
def simplify_network_from_gdf(gdf):
    net = Network(edges=gdf)
    net = clean_roundabouts(net)
    net = split_edges_at_nodes_pyg(net)
    net = add_endpoints(net)
    net = add_ids(net)
    net = add_topology(net)    
    net = drop_hanging_nodes(net)    
    net = merge_2(net)
    net =reset_ids(net) 
    net = add_distances(net)
    net = merge_all_multi(net)
    logicCheck(net)
    net =quickFix(net)
    net = add_travel_time(net)
    #with Geopackage('final.gpkg', 'w') as out:
        #out.add_layer(net.nodes, name='nodes', crs='EPSG:4326')
        #out.add_layer(net.edges, name="edges",crs='EPSG:4326')
      
    return net


#designed with the addition of ferries in mind, to snap eligible routes onto existing network
#with special logic for loading unloading, left after other methods to protect from merge
#splitting and dropping logic. keeps these edges seperate from road simplification. only issue
#is the snapping threshold needs to be more forgiving as often nearest nodes have been merged away
#worth looking at edge finding in some cases. also seems to be a good idea to 
#ferries will have their own time calculation method
def add_modal(gdf,alter_transport,threshold=0.02):
    edges = gdf.edges.copy()
    nodes = gdf.nodes.copy()
    node_degree = nodes.degree.to_numpy()
    sindex_nodes = pygeos.STRtree(nodes['geometry'])
    sindex_edges = pygeos.STRtree(edges['geometry'])
    new_edges = []
    edge_id_counter = len(edges)
    counter = 0
    for route in alter_transport.itertuples():
        route_geom = route.geometry
        start = pygeom.get_point(route_geom,0)
        end = pygeom.get_point(route_geom,-1)

        near_start = _intersects_pyg(start,edges['geometry'],sindex_edges, tolerance=threshold)
        near_end = _intersects_pyg(end,edges['geometry'],sindex_edges, tolerance=threshold)
        near_start = near_start.index.values
        near_end = near_end.index.values
        print(near_end)
        print(near_start)
        if len(near_start) < 1 or len(near_end) < 1: continue

        if len(near_start) > 1: 
            near_start = min([edges.iloc[match_idx] for match_idx in near_start],
                key=lambda match: pygeos.distance(start,match.geometry))
            near_start = near_start.id

        else: near_start = edges.id.iloc[near_start[0]]
        if len(near_end) > 1: 
            near_end = min([edges.iloc[match_idx] for match_idx in near_end],
                key=lambda match: pygeos.distance(end,match.geometry))
            near_end=near_end.id
        else: near_end = edges.id.iloc[near_end[0]]
        if near_end==near_start: 
            print("for counter ", counter, "we skipped")
            continue
        #pick nodes to create edge
        near_start = edges.iloc[near_start]
        near_end = edges.iloc[near_end]
        new_line_start = pygeos.coordinates.get_coordinates(route_geom)

        from_is_closer = pygeos.measurement.distance(start, nodes.iloc[near_start.from_id].geometry) < pygeos.measurement.distance(start, nodes.iloc[near_start.to_id].geometry)
        if from_is_closer:
            start_id = near_start.from_id
        else:
            start_id = near_start.to_id
        node_degree[start_id] += 1
        new_line = np.concatenate((pygeos.coordinates.get_coordinates(nodes.iloc[start_id].geometry),new_line_start))
        from_is_closer = pygeos.measurement.distance(end, nodes.iloc[near_end.from_id].geometry) < pygeos.measurement.distance(end, nodes.iloc[near_end.to_id].geometry)
        if from_is_closer:
            end_id = near_end.from_id
        else:
            end_id = near_end.to_id
        node_degree[end_id] += 1
        new_line = np.concatenate((new_line,pygeos.coordinates.get_coordinates(nodes.iloc[end_id].geometry)))
        new_edges.append({'osm_id':route.osm_id,'geometry': pygeos.linestrings(new_line),'highway':route.highway,'id':edge_id_counter,'from_id':start_id,'to_id':end_id,'distance':999,'time':999})
    
    

        counter+=1
        
    edges = edges.append(new_edges,ignore_index=True)
    edges.reset_index(inplace=True)
    return Network(edges = edges, nodes=nodes)
        



#def snap_pyg(edge,nodes):


def logicCheck(net):
    nodes = net.nodes.copy()
    edges = net.edges.copy()

    indexRef=False
    eID = []
    nID = []
    if max(edges.from_id) > max(nodes.id) or max(edges.to_id) > max(nodes.id):
        print("ERROR: From or to id out of index")
        print("max node id: ", max(nodes.id))
        print("max from id: ", max(edges.from_id))
        print("max to id: ", max(edges.to_id))

    cur_deg = nodes['degree'].to_numpy()
    cal_deg = ['1','2']
    try:
        cal_deg = calculate_degree(net)
    except: print("Degree could not be calculated from from and to ids")

    if not np.array_equal(cur_deg,cal_deg): print("Final node degree values do not correspond to edge dataframe")


    bugs = net.edges.loc[edges.id.isin(eID)]

    bugN = net.nodes.loc[nodes.id.isin(nID)]
    try:
        with Geopackage('bugs.gpkg', 'w') as out:
            out.add_layer(net.edges, name='ed', crs='EPSG:4326')
            out.add_layer(net.nodes,name='no',crs='EPSG:4326')
    except:
        print("eh")

def findMulti(net):
    edges = net.edges.copy()
    multi = []
    for edge in edges.itertuples():
        if pygeom.get_type_id(edge.geometry) == '5':
            multi.append(edge.id)
    line = edges.loc[~edges.id.isin(multi)]
    multiline = edges.loc[edges.id.isin(multi)]
    #try:
     #   with Geopackage('multi.gpkg', 'w') as out:
      #      out.add_layer(line, name='l', crs='EPSG:4326')
            #out.add_layer(multiline,name='m',crs='EPSG:4326')
    #except: 
    print(len(multi), " multilines found")
 

def quickFix(net):
    edges = net.edges.copy()
    a = []
    rem = []
    for edge in edges.itertuples():
        if not pygeos.get_num_geometries(edge.geometry) ==1:
            b = pygeos.get_num_geometries(edge.geometry)
            print("Multiple geometries in edge id: ", edge.id)
            for x in range(b):
                a.append(pygeom.get_geometry(edge.geometry,x)) 
           
            rem.append(edge.id)
        if edge.from_id > max(net.nodes.id) or edge.to_id > max(net.nodes.id):
            rem.append(edge.id)
    edges = edges.loc[~edges.id.isin(rem)]
    edges['id'] = range(len(edges))
    edges.reset_index(drop=True,inplace=True)
    #a should have the individual linestrings in it 
    return Network(edges=edges,nodes=net.nodes)

#Creates an igraph from geodataframe with the distances as weights. 
def igraph_from_gdf(gdf):
    net = simplify_network_from_gdf(gdf)
    g = ig.Graph.TupleList(net.edges[['from_id','to_id','distance']].itertuples(index=False))
    #layout = g.layout("kk")
    #ig.plot(g, layout=layout)
    return g

def subsection(network):
    e = network
    return d_within(e.iloc[221].geometry, e, 0.03)

