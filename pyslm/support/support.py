"""
Provides classes  and methods for the creation of support structures in Additive Manufacturing.
"""

try:
    import triangle
except BaseException as E:
    raise BaseException("Lib Triangle is required to use support.geometry submodule")

try:
    import mapbox_earcut
except BaseException as E:
    raise BaseException("Mapbox earcut is required to use the support.geometry submodule")

try:
    import vispy
except BaseException as E:
    raise BaseException("Vispy is required to use the support.geometry submodule")

import abc
from builtins import staticmethod
from typing import Any, Optional, List, Tuple, Union
import subprocess
import logging
import time
import warnings

import scipy.ndimage.filters
from scipy import interpolate
from skimage.measure import find_contours

import shapely.geometry
import shapely.affinity
from shapely.geometry import Polygon, MultiPolygon

import numpy as np
import trimesh
import trimesh.path
import trimesh.path.traversal

import pyslm.support.geometry
import pyslm.support.render
import pyclipr

from ..core import Part
from .utils import *
from .geometry import *
from ..hatching import BaseHatcher, utils
import pyslm.hatching.utils as hatchingUtils


class SupportStructure(abc.ABC):
    """
    The Support Structure is the base class representing all definitions of support structures used in PySLM.
    This includes attributes that link to the source part and the original support faces or features of an object and
    shared utility methods for the calculation of useful properties.

    It stores properties that describe the type of support identified during its generation and stores relevant data
    connecting this such as the original support surface (:attr:`supportSurface`) and
    the support object or part (:attr:`supportObject`) and if the support self-intersects with
    the part (:attr:`supportObject`).
    """
    def __init__(self,
                 supportObject: Part = None,
                 supportVolume: trimesh.Trimesh = None,
                 supportSurface: trimesh.Trimesh = None,
                 intersectsPart: bool = False):

        self._supportVolume = supportVolume
        self._supportObject = supportObject
        self._supportSurface = supportSurface
        self._intersectsPart = intersectsPart

    def __str__(self):
        return 'SupportStructure'

    @abc.abstractmethod
    def geometry(self) -> trimesh.Trimesh:
        """
        Returns the geometry representing the support structure
        """
        raise NotImplementedError('Geometry property is an abstract method')

    @staticmethod
    def flattenSupportRegion(region):
        """
        The function takes a support surfaces and flattens this as a projected polygon.

        :param region: The support surface as a :class:`trimesh.Trimesh` mesh
        :return: The 2D Polygon of the flattened surface
        """

        supportRegion = region.copy()

        """ Extract the outline of the overhang mesh region"""
        poly = supportRegion.outline()

        """ Convert the line to a 2D polygon"""
        poly.vertices[:, 2] = 0.0

        flattenPath, polygonTransform = poly.to_planar()
        flattenPath.process()

        flattenPath.apply_translation(polygonTransform[:2, 3])  # np.array([polygonTransform[0, 3],

        #flattenPath = flattenPath.simplify_spline(smooth=1000)
        # polygonTransform[1, 3]]))
        polygon = flattenPath.polygons_full[0]

        return polygon

    @property
    @abc.abstractmethod
    def volume(self) -> float:
        """
        Returns the  volume of the Support Geometry
        """
        raise NotImplementedError('Support Volume property is an abstract method')

    def projectedSupportArea(self) -> float:
        """
        Convenience function returns the total projected surface area of the support.

        :return:  The total projected (flattened) surface support area
        """
        if self._supportSurface:
            return self.flattenSupportRegion(self._supportSurface).area
        else:
            return 0.0

    def supportArea(self) -> float:
        """
        Convenience function returns the total surface area  of the support region.

        :return:  The total surface area of the support
        """

        return self._supportSurface.area if self._supportSurface else 0.0

    @property
    def intersectsPart(self) -> bool:
        """ Indicates the projected support structure intersect with the originating part """
        return self._intersectsPart

    @intersectsPart.setter
    def intersectsPart(self, state : bool):
        self._intersectsPart = state

    @property
    def supportSurface(self) -> trimesh.Trimesh:
        """ The support surface identified on the originating part """
        return self._supportSurface

    @supportSurface.setter
    def supportSurface(self, surface: trimesh.Trimesh):
        self._supportSurface = surface

    @property
    def supportObject(self) -> Any:
        """ The originating object that the support structure is generated for """
        return self._supportObject

    @supportObject.setter
    def supportObject(self, obj: Any):
        self._supportObject = obj


class BlockSupportBase(SupportStructure):
    """
    The BlockSupportBase is a base class representing **a single support volume** region constructed by an extruded
    overhang surface region, that may intersect with the build platform (:math:`z=0`) or self-intersect with the original
    mesh of the :class:`Part`.

    These are generated externally in the :class:`BlockSupportGenerator` and other derived generator classes that
    build upon this. Objects represent the data structure for the support strucutre rather than the methods for
    generating themselves.

    The support volume (:attr:`supportVolume` is a generic 3D volume body or mesh that enables
    differentiation of support structures to be generated by creating a derived class that re-defines the
    abstract method :meth:`SupportStructure.geometry`.
    """

    def __init__(self,
                 supportObject: Part = None,
                 supportVolume: trimesh.Trimesh = None,
                 supportSurface: trimesh.Trimesh = None,
                 intersectsPart: bool = False):

        super().__init__(supportObject, supportVolume, supportSurface, intersectsPart)

    def __str__(self):
        return 'BlockSupportBase'

    def geometry(self) -> trimesh.Trimesh:
        """
        Returns the geometry representing the support structure.
        """
        return self._supportVolume

    @property
    def volume(self) -> float:
        """ The calculated volume of the support volume region """
        return self._supportVolume.volume

    @property
    def supportVolume(self) -> trimesh.Trimesh:
        """
        The support volume stores the 3D mesh geometry representing an extruded geometry projected onto either the
        part surface or build-plate (:math:`z=0`). This is generated externally in :class:`BlockSupportGenerator` and the
        resultant block 3D geometry is stored in this property.
        """
        return self._supportVolume

    @supportVolume.setter
    def supportVolume(self, supportVolume: trimesh.Trimesh):
        self._supportVolume = supportVolume

    @property
    def supportBoundary(self) -> trimesh.Trimesh:
        """
        The boundary or vertical walls constructed from the extruded support volume. These are identified by
        taking using :meth:`utils.getFaceZProjectionWeight` and then using a default threshold value in
        the private static attribute :attr:`BlockSupportBase._supportSkinSideTolerance`

        .. note::
            Any self-intersections with the object geometry that are steep (~90 degrees) may potentially be included.
        """

        blockSupportSides = self._supportVolume.copy()
        sin_theta = getFaceZProjectionWeight(blockSupportSides)

        blockSupportSides.update_faces(sin_theta > (1.0-1e-4))
        blockSupportSides.remove_unreferenced_vertices()

        return blockSupportSides

    @staticmethod
    def triangulateSections(sections) -> trimesh.Trimesh:
        """
        A static method to take a collection of section slice or cross-section and triangulate them into a combined
        mesh. The triangulated meshed are then transformed based on the original transformation generated internally
        when using :meth:`trimesh.Trimesh.section`.

        :param sections: The sections to triangulate into a mesh
        :return: A mesh containing the  concatenated triangulated polygon sections
        """
        sectionMesh = trimesh.Trimesh()

        for section in sections:
            if section is None:
                continue

            v, f = section.triangulate()

            if len(v) == 0:
                continue

            v = np.insert(v, 2, values=0.0, axis=1)
            sec = trimesh.Trimesh(vertices=v, faces=f)
            sec.apply_transform(section.metadata['to_3D'])
            sectionMesh += sec

        return sectionMesh


class BaseSupportGenerator(abc.ABC):
    """
    The BaseSupportGeneration class provides common methods used for generating the support structures
    (:class:`SupportStructure`) typically used in Additive Manufacturing.

    This class provides the base methods used for identifying geometrical unsupported features
    such as vertices and edges within a part.
    """

    PYCLIPPER_SCALEFACTOR = 1e4
    """
    The scaling factor used for polygon clipping and offsetting in `pyclipr <https://github.com/drlukeparry/pyclipr>`_ 
    for the decimal component of each polygon coordinate. This should be set to inverse of the required decimal 
    tolerance i.e. `0.01` requires a minimum scale factor of `1e2`. Default is `1e4`.
    """

    POINT_OVERHANG_TOLERANCE = 0.05
    """
    The point overhang tolerance is used for determining if adjacent connected vertices in the mesh lies above, 
    which indicates that this vertex requires an additional point support generating.
    """

    def __init__(self):
        pass

    def __str__(self):
        return 'BaseSupportGenerator'

    @staticmethod
    def findOverhangPoints(part: Part) -> np.ndarray:
        """
        Identifies vertices that require additional support based on their connectivity with adjacent vertices.

        :param part: The part to locate un-support vertices
        :return: Identified points that require additional support
        """
        meshVerts = part.geometry.vertices
        vAdjacency = part.geometry.vertex_neighbors

        pointOverhangs = []
        for i in range(len(vAdjacency)):

            # Find the edge deltas between the points
            v = meshVerts[i]
            neighborVerts = meshVerts[vAdjacency[i], :]
            delta = neighborVerts - v
            # mag = np.sqrt(np.sum(delta * delta, axis=1))
            # theta = np.arcsin(delta[:,2]/mag)
            # theta = np.rad2deg(theta)
            # if np.all(theta > -0.001):
            # pointOverhang.append(i)

            """
            If all neighbouring connected vertices lie above the point, this indicates the vertex lies below and 'may'
            not have underlying connectivity. There are two cases that exist: on upwards or downwards pointing surface.
            """
            if np.all(delta[:, 2] > -BaseSupportGenerator.POINT_OVERHANG_TOLERANCE):

                # Check that the vertex normal is pointing downwards (-ve Z) showing that the no material is underneath
                if part.geometry.vertex_normals[i][2] < 0.0:
                    pointOverhangs.append(i)

        return pointOverhangs

    @staticmethod
    def findOverhangEdges(part: Part,
                          overhangAngle: Optional[float] = 45.0,
                          edgeOverhangAngle: Optional[float] = 10.0):
        """
        Identifies edges which requires additional support based on both the support surface and support edge angle.

        :param part: The part to be analysed
        :param overhangAngle: The support surface overhang angle (degrees)
        :param edgeOverhangAngle: The edge overhang angle (degrees)

        :return: A list of edge tuples.
        """

        mesh = part.geometry
        edges = mesh.edges_unique
        edgeVerts = mesh.vertices[edges]

        """
        Calculate the face angles with respect to the +z vector  and the inter-face angles
        """
        theta = getSupportAngles(part, np.array([[0., 0., 1.0]]))
        adjacentFaceAngles = np.rad2deg(mesh.face_adjacency_angles)

        overhangEdges = []
        # Iterate through all the edges in the model
        for i in range(len(edgeVerts)):

            """
            Calculate the 'vertical' angle of the edge pointing in the z-direction by using the z component.
            First calculate vector, magnitude and the vertical angle of the vector
            """
            edge = edgeVerts[i].reshape(2, 3)
            delta = edge[0] - edge[1]
            mag = np.sqrt(np.sum(delta * delta))
            ang = np.rad2deg(np.arcsin(delta[2] / mag))

            # Identify if the vertical angle of the edge is less than the edgeOverhangAngle irrespective of the actual
            # direction of the vector (bidirectional)
            if np.abs(ang) < edgeOverhangAngle:

                """
                Locate the adjacent faces in the model using the face-adjacency property to identify if the edge
                belongs to a sharp corner which tends to be susceptible areas. This is done by calculating the angle
                between faces.
                """
                adjacentFaces = mesh.face_adjacency[i]

                if adjacentFaceAngles[i] > overhangAngle and np.all(theta[adjacentFaces] > 89):
                    overhangEdges.append(edges[i])

        return overhangEdges


class BlockSupportGenerator(BaseSupportGenerator):
    """
    The BlockSupportGenerator class provides common methods used for generating the 'support' structures typically used
    in Additive Manufacturing. Derived classes can build directly upon this by either using existing BlockSupports
    generated or redefining the overall support geometry created underneath overhang regions.

    After passing the geometry and setting the required parameters, the user is required to call
    :meth:`identifySupportRegions` in order to generate the support volumes.

    In summary, the technique identifies first overhang surface regions on the mesh geometry provided based on the
    chosen :attr:`overhangAngle`.
    From these identified overhang surfaces,  extruded prisms are generated and are then intersected with the original
    part in both :math:`+Z` and :math:`-Z` directions using the Boolean CSG library (manifold). This provides a method
    to approximately  isolate regions and fundamentally decide if the supports are self-intersecting
    (:attr:`SupportStructure.intersectsPart`). Non-intersecting regions are connected to the build-plate only and are
    excluded from further processing.

    Each of these regions are then ray traced using an OpenGL depth technique to identify the support regions using the
    private method :meth:`_identifySelfIntersectionHeightMap`.

    Intersecting regions with the part are identified and these can be more smartly separated based on a tolerance
    :meth:`gradThreshold` calculated from the equivalent ray or rasterised projection resolution
    :attr:`rayProjectionResolution` and :attr:`overhangAngle` previously defined. Regions identified are
    simplified from a calculated heightMap image and then approximate support extrusions are generated that intersect
    with the originating part by adding Z-offsets (:attr:`lowerProjectionOffset` and :attr:`upperProjectedOffset`).

    Finally, these extruded regions are intersected with the part using the CSG library to produce the final
    :class:`BlockSupportBase` that precisely conforms the boundary of the part if there are self-intersections.
    """

    _supportSkinSideTolerance = 1.0 - 1e-3
    """
    The support skin side tolerance is used for masking the extrusions side faces when generating the polygon region
    for creating the surrounding support skin. 
    
    By masking the regions, the upper and lower surfaces of the extruded
    volume are separated and their 3D boundaries can be extracted.
    """

    _intersectionVolumeTolerance = 50
    """
    An internal tolerances used to determine if the projected volume intersects with the part
    """

    _gaussian_blur_sigma = 1.0
    """
    The internal parameter is used for blurring the calculated depth field to smooth out the boundaries. Care should
    be taken to keep this low as it will artificially offset the boundary of the support
    """

    def __init__(self):

        super().__init__()

        self._minimumAreaThreshold = 5.0  # mm2 (default = 10)
        self._rayProjectionResolution = 0.2  # mm (default = 0.5)

        self._lowerProjectionOffset = 0.05 # mm
        self._upperProjectionOffset = 0.05 # mm

        self._innerSupportEdgeGap = 0.2  # mm (default = 0.1)
        self._outerSupportEdgeGap = 0.5  # mm  - offset between part supports and baseplate supports

        self._triangulationSpacing = 2  # mm (default = 1)
        self._simplifyPolygonFactor = 0.5

        self._overhangAngle = 45.0  # [deg]

        self._useApproxBasePlateSupport = False  # default is false
        self._splineSimplificationFactor = 20.0

    def __str__(self):
        return 'BlockSupportGenerator'

    @staticmethod
    def gradThreshold(rayProjectionDistance: float, overhangAngle: float) -> float:
        """
        A static method which defines the threshold  applied to the gradient generated from the support
        depth map, which separates each support volume region. This is based on a combination of the ray projection
        resolution, the overhang angle and an arbitrary constant to ensure discrete regions are isolated.

        :param rayProjectionDistance: The ray projection resolution used
        :param overhangAngle: The overhang angle [degrees]
        :return: The gradient threshold used.

        """
        return 5.0 * np.tan(np.deg2rad(overhangAngle)) * rayProjectionDistance

    @property
    def splineSimplificationFactor(self) -> float:
        """
        The simplification factor using a spline approximation approach for smoothening the support volume boundary
        """
        return self._splineSimplificationFactor

    @splineSimplificationFactor.setter
    def splineSimplificationFactor(self, value: float):
        self._splineSimplificationFactor = value

    @property
    def overhangAngle(self) -> float:
        """ The overhang angle (degrees) used for identifying support surfaces on the :class:`Part` """
        return self._overhangAngle

    @overhangAngle.setter
    def overhangAngle(self, angle: float):
        self._overhangAngle = angle

    @property
    def upperProjectionOffset(self) -> float:
        """
        An internal parameter used for defining an offset applied to the upper projection used to provide a clean
        intersection when performing the final boolean intersection between the original geometry and the extruded
        support volume geometry.
        """
        return self._upperProjectionOffset

    @upperProjectionOffset.setter
    def upperProjectionOffset(self, offset: float) -> None:
        self._upperProjectionOffset = offset

    @property
    def lowerProjectionOffset(self) -> float:
        """
        The offset applied to the lower projection used to provide a clean intersection when performing the final boolean
        intersection between the original geometry and the extruded support volume geometry.
        """
        return self._lowerProjectionOffset

    @lowerProjectionOffset.setter
    def lowerProjectionOffset(self, offset: float) -> None:
        self._lowerProjectionOffset = offset

    @property
    def outerSupportEdgeGap(self) -> float:
        """ The offset applied to the  projected boundary of the support volume."""
        return self._outerSupportEdgeGap

    @outerSupportEdgeGap.setter
    def outerSupportEdgeGap(self, spacing: float):
        self._outerSupportEdgeGap = spacing

    @property
    def innerSupportEdgeGap(self) -> float:
        """
        The inner support gap is the distance between adjacent supports regions that are identified as separated by a
        significant vertical extent.
        """
        return self._innerSupportEdgeGap

    @innerSupportEdgeGap.setter
    def innerSupportEdgeGap(self, spacing: float):
        self._innerSupportEdgeGap = spacing

    @property
    def minimumAreaThreshold(self) -> float:
        """
        The minimum support area threshold (:math:`mm^2`) used to identify disconnected support regions.
        Support regions with a smaller area will be excluded and not generated.
        """
        return self._minimumAreaThreshold

    @minimumAreaThreshold.setter
    def minimumAreaThreshold(self, areaThresholdValue: float):
        self._minimumAreaThreshold = areaThresholdValue

    @property
    def simplifyPolygonFactor(self) -> float:
        """
        The simplification factor used for simplifying the boundary polygon generated from the rasterisation process.
        This has the effect of reducing the complexity of the extruded support volume generated that is intersected with
        the :class:`Part`.
        """
        return self._simplifyPolygonFactor

    @simplifyPolygonFactor.setter
    def simplifyPolygonFactor(self, value: float) -> None:
        self._simplifyPolygonFactor = value

    @property
    def triangulationSpacing(self) -> float:
        """ The spacing factor used whilst triangulating the support polygon region."""
        return self._triangulationSpacing

    @triangulationSpacing.setter
    def triangulationSpacing(self, spacing: float) -> None:
        self._triangulationSpacing = spacing

    @property
    def rayProjectionResolution(self) -> float:
        """
        The equivalent ray projection resolution used to discretise the projected support region using the OpenGL
        rasterisation. This can be adjusted accordingly depending on the overall scale and size of the Part,
        although this is mostly insignificant due to the relatively high performance using OpenGL.

        The resolution should be selected to appropriately capture the complexity of the features within the part.

        .. note::
            There is a restriction on the maximum size based on the framebuffer memory available in the OpenGL context
            provided by the chosen Operating System and drivers
        """
        return self._rayProjectionResolution

    @rayProjectionResolution.setter
    def rayProjectionResolution(self, resolution: float) -> None:
        self._rayProjectionResolution = resolution

    def filterSupportRegion(self, region):
        """ Not implemented """
        raise Exception('Not Implemented')

    def generateIntersectionHeightMap(self):
        """ Not implemented """
        raise Exception('Not Implemented')

    def _identifySelfIntersectionHeightMap(self, subregion: trimesh.Trimesh,
                                           offsetPoly: trimesh.path.Path2D,
                                           cutMesh: trimesh.Trimesh,
                                           bbox: np.ndarray) -> Tuple[np.ndarray]:
        """
        Generates the height map of the upper and lower depths. This is done by projecting rays at a resolution
        (attr:`~BlockSupportGenerator.rayProjectionResolution`) across the entire polygon region (offsetPoly) in both
        vertical directions (+z, -z) and are intersected with the upper and lower support surface. A sequence of
        height maps are generated from these ray intersections.

        :param subregion: The upper surface (typically overhang surface region)
        :param offsetPoly: The polygon region defining the support region
        :param cutMesh: The lower intersecting surfaces which potentially intersect with the polygon region
        :return: A tuple containing various height maps
        """

        logging.info('\tGenerated support height map (OpenGL Version)')

        # Extend the bounding box extents in the Z direction
        bboxCpy = bbox.copy()
        bboxCpy[0,2] -= 1
        bboxCpy[1,2] += 1

        upperImg = pyslm.support.render.projectHeightMap(subregion, self.rayProjectionResolution, False, bboxCpy)

        # Cut mesh is lower surface
        lowerImg = pyslm.support.render.projectHeightMap(cutMesh, self.rayProjectionResolution, True, bboxCpy)
        lowerImg = np.flipud(lowerImg)

        # Generate the difference between upper and lower ray-traced intersections
        heightMap2 = upperImg.copy()
        mask = lowerImg > 1.01
        heightMap2[mask] = lowerImg[mask]

        return heightMap2.T, upperImg, lowerImg

    def _identifySelfIntersectionHeightMapRayTracing(self, subregion: trimesh.Trimesh,
                                                     offsetPoly: trimesh.path.Path2D,
                                                     cutMesh: trimesh.Trimesh) -> Tuple[np.ndarray]:
        """
        Deprecated: Generates the height map of the upper and lower depths. This is done by projecting rays at a resolution
        (attr:`~BlockSupportGenerator.rayProjectionResolution`) across the entire polygon region (offsetPoly) in both
        vertical directions (+z, -z) and are intersected with the upper and lower support surface. A sequence of
        height maps are generated from these ray intersections.

        :param subregion: The upper surface (typically overhang surface region)
        :param offsetPoly: The polygon region defining the support region
        :param cutMesh: The lower intersecting surfaces which potentially intersect with the polygon region
        :return: A tuple containing various height maps
        """

        # Rasterise the surface of overhang to generate projection points
        supportArea = np.array(offsetPoly.rasterize(self.rayProjectionResolution, offsetPoly.bounds[0, :])).T

        coords = np.argwhere(supportArea).astype(np.float32) * self.rayProjectionResolution
        coords += offsetPoly.bounds[0, :] + 1e-5  # An offset is required due to rounding error

        logging.warning('Depreceated function')
        logging.info('\t - start projecting rays')
        logging.info('\t - number of rays with resolution ({:.3f}): {:d}'.format(self.rayProjectionResolution, len(coords)))

        """
        Project upwards to intersect with the upper surface
        """
        # Set the z-coordinates for the ray origin
        coords = np.insert(coords, 2, values=-1e5, axis=1)
        rays = np.repeat([[0., 0., 1.]], coords.shape[0], axis=0)

        # Find the first location of any triangles which intersect with the part
        hitLoc, index_ray, index_tri = subregion.ray.intersects_location(ray_origins=coords,
                                                                         ray_directions=rays,
                                                                         multiple_hits=False)
        logging.info('\t - finished projecting rays')

        coords2 = coords.copy()

        coords2[index_ray, 2] = 1e7
        rays[:, 2] = -1.0

        # If any verteces in triangle there is an intersection
        # Find the first location of any triangles which intersect with the part
        hitLoc2, index_ray2, index_tri2 = cutMesh.ray.intersects_location(ray_origins=coords2,
                                                                          ray_directions=rays,
                                                                          multiple_hits=False)

        logging.info('\t - finished projecting rays')

        # Create a height map of the projection rays
        heightMap = np.ones(supportArea.shape) * -1.0

        heightMapUpper = np.zeros(supportArea.shape)
        heightMapLower = np.zeros(supportArea.shape)

        if len(hitLoc) > 0:
            hitLocCpy = hitLoc.copy()
            hitLocCpy[:, :2] -= offsetPoly.bounds[0, :]
            hitLocCpy[:, :2] /= self.rayProjectionResolution

            hitLocIdx = np.ceil(hitLocCpy[:, :2]).astype(np.int32)

            # Assign the heights
            heightMap[hitLocIdx[:, 0], hitLocIdx[:, 1]] = hitLoc[:, 2]
            heightMapUpper[hitLocIdx[:, 0], hitLocIdx[:, 1]] = hitLoc[:,2]

        if len(hitLoc2) > 0:
            hitLocCpy2 = hitLoc2.copy()
            # Update the xy coordinates
            hitLocCpy2[:, :2] -= offsetPoly.bounds[0, :]
            hitLocCpy2[:, :2] /= self.rayProjectionResolution
            hitLocIdx2 = np.ceil(hitLocCpy2[:, :2]).astype(np.int32)
            # Assign the heights based on the lower projection
            heightMap[hitLocIdx2[:, 0], hitLocIdx2[:, 1]] = hitLoc2[:, 2]
            heightMapLower[hitLocIdx2[:, 0], hitLocIdx2[:, 1]] = hitLoc2[:, 2]

        logging.info('\tgenerated support height map')

        return heightMap, heightMapUpper, heightMapLower


    def identifySupportRegions(self, part: Part, overhangAngle: float,
                               findSelfIntersectingSupport: Optional[bool] = True) -> List[BlockSupportBase]:
        """
        Extracts the overhang mesh and generates block regions given a part and target overhang angle. The algorithm
        uses a combination of boolean operations and ray intersection/projection to discriminate support regions.
        If :code:`findSelfIntersectingSupport` is to set :code:`True` (default), the algorithm will process and
        separate overhang regions that by downward projection self-intersect with the part.

        This provides more refined behavior than simply projected support material downwards into larger support
        block regions and separates an overhang surface between intersecting and non-intersecting regions.

        :param part: Part for generating support structures for
        :param overhangAngle: Overhang angle (degrees)
        :param findSelfIntersectingSupport: Generates supports that intersect with the part

        :return: A list of BlockSupports
        """

        overhangSubregions = getOverhangMesh(part, overhangAngle, True)

        """
        The geometry of the part requires exporting as a '.off' file to be correctly used with the Cork Library
        """

        supportBlockRegions = []

        totalBooleanTime = 0.0

        """ Process sub-regions"""
        for subregion in overhangSubregions:

            logging.info('Processing subregion')
            try:
                polygon = SupportStructure.flattenSupportRegion(subregion)
            except:
                logging.warning('PySLM: Could not flatten region')
                continue

            #mergedPoly = trimesh.load_path(outline)
            #mergedPoly.merge_vertices(1)
            #mergedPoly = mergedPoly.simplify_spline(self._splineSimplificationFactor)

            # Simplify the polygon to ease simplify extrusion

            # Offset in 2D the support region projection

            offsetShape = polygon.simplify(self.simplifyPolygonFactor, preserve_topology=False).buffer(-self.outerSupportEdgeGap)

            if offsetShape is None or offsetShape.area < self.minimumAreaThreshold:
                logging.info('\t - Note: skipping shape (area too small)')
                continue

            if isinstance(offsetShape, shapely.geometry.MultiPolygon):
                offsetPolyList = []
                for poly in offsetShape.geoms:
                    triPath = trimesh.load_path(poly, process=False)#.simplify_spline(self._splineSimplificationFactor)
                    if triPath.is_closed and triPath.area > self.minimumAreaThreshold:

                        offsetPolyList.append(triPath)

                if not offsetPolyList:
                    logging.info('\t - Note: skipping shape - no valid regions identified')
                    continue

                offsetPolys = offsetPolyList[0]

                for poly in offsetPolyList[1:]:
                    offsetPoly += poly

            else:
                offsetPoly = trimesh.load_path(offsetShape)#.simplify_spline(self._splineSimplificationFactor)

            """
            Create an extrusion at the vertical extent of the part and perform self-intersection test
            """
            extruMesh2Flat = subregion.copy();
            extruMesh2Flat.vertices[:,2] = 0.0

            extruMesh2 = trimesh.creation.extrude_triangulation(extruMesh2Flat.vertices[:,:2], extruMesh2Flat.faces, 100)
            eMesh2Idx = extruMesh2.vertices[:,2] > 1.0
            extruMesh2.vertices[eMesh2Idx,2] = subregion.vertices[:,2] - 0.01
            extruMesh = extruMesh2
            #extruMesh = extrudeFace(subregion, 0.0)
            #extruMesh.vertices[:, 2] = extruMesh.vertices[:, 2] - 0.01

            timeIntersect = time.time()

            logging.info('\t - start intersecting mesh')

            bbox = extruMesh.bounds
            cutMesh = boolIntersect(part.geometry, extruMesh)
            logging.info('\t\t - Mesh intersection time using Cork: {:.3f}s'.format(time.time() - timeIntersect))
            logging.info('\t -  Finished intersecting mesh')
            totalBooleanTime += time.time() - timeIntersect

            # Note this a hard tolerance
            if cutMesh.volume < BlockSupportGenerator._intersectionVolumeTolerance: # 50

                if self._useApproxBasePlateSupport:
                    """
                    Create a support structure that extends to the base plate (z=0)
    
                    NOTE - not currently used - edge smoothing cannot be performed despite this being a
                    quicker methods, it suffer sever quality issues with jagged edges so should be avoided.
                    """
                    logging.info('Creating Approximate Base-Plate Support')

                    extruMesh.visual.face_colors[:, :3] = np.random.randint(254, size=3)

                    # Create a support block object
                    baseSupportBlock = BlockSupportBase(supportObject=part,
                                                        supportVolume=extruMesh,
                                                        supportSurface=subregion)

                    supportBlockRegions.append(baseSupportBlock)

                    continue  # No self intersection with the part has taken place with the support
            elif not findSelfIntersectingSupport:
                continue

            v0 = np.array([[0., 0., 1.0]])

            # Identify Support Angles
            v1 = cutMesh.face_normals
            theta = np.arccos(np.clip(np.dot(v0, v1.T), -1.0, 1.0))
            theta = np.degrees(theta).flatten()

            cutMeshUpper = cutMesh.copy()
            cutMeshUpper.update_faces(theta < 89.95)
            cutMeshUpper.remove_unreferenced_vertices()

            # Toggle to use full intersecting mesh
            # cutMeshUpper = cutMesh

            # Use a ray-tracing approach to identify self-intersections. This provides a method to isolate regions that
            # either are self-intersecting or not.

            logging.info('\t - start generated support height map')
            heightMap, heightMapUpper, heightMapLower = self._identifySelfIntersectionHeightMap(subregion, offsetPoly, cutMeshUpper, bbox)
            logging.info('\t - finished generated support height map')

            heightMap = np.pad(heightMap, ((2, 2), (2,2)), 'constant', constant_values=((1, 1), (1,1)))

            import matplotlib.pyplot as plt

            vx, vy = np.gradient(heightMap)
            grads = np.sqrt(vx ** 2 + vy ** 2)

            grads = scipy.ndimage.filters.gaussian_filter(grads, sigma=BlockSupportGenerator._gaussian_blur_sigma)

            """
            Find the outlines of any regions of the height map which deviate significantly
            This is used to separate both self-intersecting supports and those which are simply connected
            to the base-plate.
            """
            outlines = find_contours(grads, self.gradThreshold(self.rayProjectionResolution, self.overhangAngle),
                                     mask=heightMap > 2)

            # Transform the outlines from image to global coordinates system
            outlinesTrans = []
            for outline in outlines:
                outlinesTrans.append(outline * self.rayProjectionResolution + bbox[0, :2])

            # Convert outlines into closed polygons
            outlinePolygons = pyslm.hatching.utils.pathsToClosedPolygons(outlinesTrans)

            polygons = []

            # Process the outlines found from the contours
            for outline in outlinePolygons:

                """
                Process the outline by finding the boundaries
                """
                #outline = outline * self.rayProjectionResolution + bbox[0, :2]
                #outline = pyslm.hatching.simplifyBoundaries(outlines)[0]

                #if outline.shape[0] < 3:
                #    continue

                """
                Process the polygon  by creating a shapley polygon and offseting the boundary
                """
                mergedPoly = trimesh.load_path(outline)
                mergedPoly.merge_vertices(1)

                mergedPoly = mergedPoly.simplify_spline(self._splineSimplificationFactor)

                try:
                    outPolygons = mergedPoly.polygons_full
                except:
                    raise Exception('Incompatible Shapely version used')

                if not mergedPoly.is_closed or len(outPolygons) == 0 or outPolygons[0] is None:
                    continue

                if len(outPolygons) > 1:
                    raise Exception('Multi polygons - error please submit a bug report')

                bufferPolyA = mergedPoly.polygons_full[0].simplify(self.simplifyPolygonFactor*self.rayProjectionResolution)

                bufferPoly = bufferPolyA.buffer(-self.innerSupportEdgeGap)

                if isinstance(bufferPoly, shapely.geometry.MultiPolygon):
                    polygons += bufferPoly.geoms
                else:
                    polygons.append(bufferPoly)

            for bufferPoly in polygons:

                if bufferPoly.area < self.minimumAreaThreshold:
                    continue

                """
                Triangulate the polygon into a planar mesh
                """
                poly_tri = trimesh.creation.triangulate_polygon(bufferPoly,
                                                                triangle_args='pa{:.3f}'.format(self.triangulationSpacing),
                                                                engine='triangle')

                """
                Project upwards to intersect with the upper surface
                Project the vertices downward (-z) to intersect with the cutMesh
                """
                coords = np.insert(poly_tri[0], 2, values=-1e-7, axis=1)
                ray_dir = np.repeat([[0., 0., 1.]], coords.shape[0], axis=0)

                # Find the first location of any triangles which intersect with the part
                hitLoc, index_ray, index_tri = subregion.ray.intersects_location(ray_origins=coords,
                                                                                 ray_directions=ray_dir,
                                                                                 multiple_hits=False)

                coords2 = coords.copy()
                coords2[index_ray, 2] = hitLoc[:, 2] + self.upperProjectionOffset

                ray_dir[:, 2] = -1.0

                """
                Intersecting with cutmesh is more efficient when projecting downwards
                """

                coords3 = coords2.copy()
                coords3[:,2] = 0.0

                if cutMesh.volume > BlockSupportGenerator._intersectionVolumeTolerance:

                    hitLoc2, index_ray2, index_tri2 = cutMeshUpper.ray.intersects_location(ray_origins=coords2,
                                                                                           ray_directions=ray_dir,
                                                                                           multiple_hits=False)
                else:
                    # Base-plate support
                    hitLoc2 = []

                if len(hitLoc) != len(coords) or len(hitLoc2) != len(hitLoc):
                    # The projections up and down do not match indicating that there maybe some flaw

                    if len(hitLoc2) == 0:
                        # Base plate
                        hitLoc2 = coords2.copy()
                        hitLoc2[:, 2] = 0.0

                        logging.info('\tCreating Base-plate support')
                    else:
                        logging.warning('PROJECTIONS NOT MATCHING - skipping support generation')
                        continue
                else:
                    coords3[index_ray2, 2] = hitLoc2[:, 2] - self.lowerProjectionOffset

                # Create the upper and lower surface from the Ray intersection
                surf2 = trimesh.Trimesh(vertices=coords2, faces=poly_tri[1], process= True)

                # Perform a simple 2D prismatic extrusion on the mesh
                ab = trimesh.creation.extrude_triangulation(surf2.vertices[:, :2], surf2.faces, 100)

                # Identify the upper and lower surfaces based on the prismatic extrusion
                lowerIdx = ab.vertices[:, 2] < 1
                upperIdx = ab.vertices[:, 2] > 1

                # Assign the coordinates for the upper and lower surface
                ab.vertices[lowerIdx] = coords2
                ab.vertices[upperIdx] = coords3

                # Reference the sup[p
                extrudedBlock = ab

                timeDiff = time.time()

                """
                Take the near net-shape support and obtain the difference with the original part to get clean
                boundaries for the support
                """

                """
                Previous mesh was used in Version 0.5. This was not necessarily required, but offers the most robust
                implementation dealing with self-intersections
                """
                #blockSupportMesh = boolDiff(part.geometry,extrudedBlock)
                extrudedBlock.fix_normals()
                extrudedBlock.merge_vertices()
                blockSupportMesh = boolDiff(extrudedBlock, cutMesh)

                logging.info('\t\t Boolean Difference Time: {:.3f}\n'.format(time.time() - timeDiff))

                totalBooleanTime += time.time() - timeDiff

                # Draw the support structures generated
                blockSupportMesh.visual.face_colors[:,:3] = np.random.randint(254, size=3)

                # Create a BlockSupport Object
                baseSupportBlock = BlockSupportBase(supportObject=part,
                                                    supportVolume=blockSupportMesh,
                                                    supportSurface=subregion,
                                                    intersectsPart=True)

                baseSupportBlock._upperSurface = surf2

                supportBlockRegions.append(baseSupportBlock)

            logging.info('\t - processed support face\n')

        logging.info('Total boolean time: {:.3f}\n'.format(totalBooleanTime))

        return supportBlockRegions


class GridBlockSupport(BlockSupportBase):
    """
    Represents a block support that internally generates a grid truss structure representing the support structure. The
    grid is generated by taking a sequence of cross-sections at a fixed distance (:attr:`~GridBlockSupport.gridSpacing`)
    from the generated support volume (:attr:`~BlockSupportBase.supportVolume`).

    The 2D slices along the grid are generated in :meth:`generateSupportSlices` and produce a 2D grid of
    cross-sections as polygons. Each polygon slice can  be designed to include  patterns such as perforation holes
    or a truss network. Potentially the 2D slices may be extruded and offset to increase the support strength,
    if required. A surrounding border with a conformal truss grid is also generated using
    :meth:`~GridBlockSupport.generateSupportSkins`.

    A truss network is generated to reduce the amount of support material processed, but additionally provides internal
    perforations which aid powder support removal after production. The truss network in 2D is generated
    along with supporting functions for creating a mesh. The generation of the truss grid is relatively expensive to
    generated compared to the polygon intersection and may be disabled by setting
    :attr:`~GridBlockSupport.generateTrussGrid`).

    Furthermore, penetrating teeth located at the upper and lower intersection of the support volume can be generated
    to ease support removal for metal AM processes. A variety of configurable options are included to vary the geometry.
    The tooth sizes may be specified using :attr:`~GridBlockSupport.supportTeethHeight`,
    :attr:`~GridBlockSupport.supportTeethTopLength`, :attr:`~GridBlockSupport.supportTeethBottomLength`,
    :attr:`~GridBlockSupport.supportTeethBaseInterval` and an additional self-penetration distance
    :attr:`~GridBlockSupport.supportTeethUpperPenetration` to enhance the strength of the support by over-
    scanning material within the solid part internally. The tooth profile is repeated across the upper edge of the
    intersected support volume using an internal overrideable function :meth:`toothProfile`. The generation of support
    teeth on the upper and lower surfaces may be individually toggled by setting
    :attr:`~GridBlockSupport.useUpperSupportTeeth` and :attr:`~GridBlockSupport.useLowerSupportTeeth` respectively.

    The truss is designed to self-intersect at set distance based on both the :attr:`trussAngle` and
    the :attr:`gridSpacing` so that they combine as a consistently connected support mesh. Upon
    generating a polygon for each support slice, this is triangulated via :meth:`triangulatePolygon`
    to create a mesh which may be sliced and hatched later. Optionally these may be combined into a single mesh, for
    exporting externally.
    """

    _pairTolerance = 1e-1
    """
    Pair tolerance used for matching upper and lower paths of the support boundary. This is an internal tolerance
    used but may be re-defined by the user."""


    def __init__(self, supportObject: Part = None,
                       supportVolume: trimesh.Trimesh = None,
                       supportSurface: trimesh.Trimesh = None,
                       intersectsPart: bool = False):

        super().__init__(supportObject, supportVolume, supportSurface, intersectsPart)

        self._gridSpacing = [3, 3] # mm
        self._useSupportBorder = True
        self._useSupportSkin = True
        self._supportWallThickness = 0.5
        self._supportBorderDistance = 3.0
        self._trussWidth = 1.0
        self._trussAngle = 45
        self._mergeMesh = False
        self._numSkinMeshSubdivideIterations = int(2)

        self._supportTeethHeight = 1.5  # mm
        self._supportTeethTopLength = 0.1 # mm
        self._supportTeethBottomLength = 1.5 # mm
        self._supportTeethBaseInterval = 0.2 # mm
        self._supportTeethUpperPenetration = 0.2 # mm

        self._useUpperSupportTeeth = True
        self._useLowerSupportTeeth = False

    def __str__(self):
        return 'GridBlockSupport'

    @property
    def numSkinMeshSubdivideIterations(self) -> int:
        """
        Number of times to subidivde the support skin mesh to increase the resolution of the mesh when conforming to
        the boundary of the support block volume. Default is 2.
        """
        return self._numSkinMeshSubdivideIterations

    @numSkinMeshSubdivideIterations.setter
    def numSkinMeshSubdivideIterations(self, iterations: int):
        self._numSkinMeshSubdivideIterations = int(iterations)

    @property
    def supportWallThickness(self):
        """
        An offset thickness applied to the lower and upper edges adjacent to the perforated teeth of the supports to
        provide additional strength
        """
        return self._supportWallThickness

    @supportWallThickness.setter
    def supportWallThickness(self, wallThickness: float ):
        self._supportWallThickness = wallThickness

    @property
    def useLowerSupportTeeth(self):
        return self._useLowerSupportTeeth

    @useLowerSupportTeeth.setter
    def useLowerSupportTeeth(self, state):
        self._useLowerSupportTeeth = state

    @property
    def useUpperSupportTeeth(self):
        return self._useUpperSupportTeeth

    @useUpperSupportTeeth.setter
    def useUpperSupportTeeth(self, state):
        self._useUpperSupportTeeth = state

    @property
    def supportTeethHeight(self) -> float:
        """
        The height of the perforated support teeth
        """
        return self._supportTeethHeight

    @supportTeethHeight.setter
    def supportTeethHeight(self, teethHeight: float):
        self._supportTeethHeight = teethHeight

    @property
    def supportTeethTopLength(self):
        """
        The upper span or length of the perforated support teeth
        """
        return self._supportTeethTopLength

    @supportTeethTopLength.setter
    def supportTeethTopLength(self, topLength: float):
        self._supportTeethTopLength = topLength

    @property
    def supportTeethBottomLength(self) -> float:
        """
        The bottom span or length of the perforated support teeth
        """
        return self._supportTeethTopLength

    @supportTeethBottomLength.setter
    def supportTeethBottomLength(self, bottomLength: float):
        self._supportTeethBottomLength = bottomLength

    @property
    def supportTeethBaseInterval(self) -> float:
        return self._supportTeethBaseInterval

    @supportTeethBaseInterval.setter
    def supportTeethBaseInterval(self, baseInterval: float):
        self._supportTeethBaseInterval = baseInterval

    @property
    def supportTeethUpperPenetration(self) -> float:
        """
        Vertical (+z) penetration of the support teeth into the intersecting mesh
        """
        return self._supportTeethUpperPenetration

    @supportTeethUpperPenetration.setter
    def supportTeethUpperPenetration(self, distance: float):
        self._supportTeethUpperPenetration = distance

    @property
    def mergeMesh(self) -> bool:
        """
        Determines if the support truss geometry should be merged together into a connected unified mesh"
        """
        return self._mergeMesh

    @mergeMesh.setter
    def mergeMesh(self, state: bool):
        self._mergeMesh = state

    @property
    def useSupportSkin(self) -> bool:
        """ Generates a support skin around the extruded boundary of the support"""
        return self._useSupportSkin

    @useSupportSkin.setter
    def useSupportSkin(self, value):
        self._useSupportSkin = value

    @property
    def useSupportBorder(self) -> bool:
        """ Generates a border around the each truss grid """
        return self._useSupportBorder

    @useSupportBorder.setter
    def useSupportBorder(self, value: bool):
        self._useSupportBorder = value

    @property
    def trussWidth(self) -> float:
        """
        The width of a strut in the truss network
        """
        return self._trussWidth

    @trussWidth.setter
    def trussWidth(self, width: float):
        self._trussWidth = width

    @property
    def generateTrussGrid(self) -> bool:
        return self._generateTrussGrid

    @generateTrussGrid.setter
    def generateTrussGrid(self, value):
        self._generateTrussGrid = value

    @property
    def supportBorderDistance(self) -> float:
        """
        The offset used when generating a border or support skin for each truss slice in the support block.
        """
        return self._supportBorderDistance

    @supportBorderDistance.setter
    def supportBorderDistance(self, distance: float):
        self._supportBorderDistance = distance

    @property
    def trussAngle(self) -> float:
        """
        The angle (degrees) used for generating the truss structures used in the support structure.
        """
        return self._trussAngle

    @trussAngle.setter
    def trussAngle(self, angle: float):
        self._trussAngle = angle

    @property
    def gridSpacing(self) -> List[float]:
        """
        The spacing of the grid truss structure within the block support .
        """
        return self._gridSpacing

    @gridSpacing.setter
    def gridSpacing(self, spacing: List[float]):
        """
        The Grid spacing used for the support structure.
        """
        self._gridSpacing = spacing

    @staticmethod
    def holeGeometry():
        """ Depreciated function """
        return Polygon([[-1.5, 0], [0, 1.], [1.5, 0], [0, -1.0], [-1.5, 0]])

    @staticmethod
    def clipLines(paths: Any, lines: np.ndarray) -> List[np.ndarray]:
        """
        Clips a series of lines (hatches) across a closed polygon or set of paths. It is an overloaded function for
        internally clipping hatches according to a pyclipr supported path.

        :param paths: The set of boundary paths for trimming the lines
        :param lines: The un-trimmed lines to clip from the boundary
        :return: A list of trimmed lines (open paths)
        """

        pc = pyclipr.Clipper()
        pc.scaleFactor = int(BaseHatcher.PYCLIPPER_SCALEFACTOR)

        pc.addPaths(lines.reshape(-1,2,3), pyclipr.Subject, True)
        pc.addPaths(paths, pyclipr.Clip, False)
        out = pc.execute(pyclipr.Intersection, pyclipr.FillRule.NonZero, returnOpenPaths=True, returnZ=False)
        lineXY = np.array(out[1])

        return lineXY

    @staticmethod
    def generateMeshGrid(poly: shapely.geometry.polygon.Polygon,
                         hatchSpacing: Optional[float] = 5.0,
                         hatchAngle: Optional[float] = 45.0) -> np.ndarray:
        """
        Generates a grid mesh i.e. a series of hatches to fill a polygon region in order to generate a truss network
        used as part of a support truss structure. The mesh grid is offset to create the truss.

        The polygon bounding box of the Shapley Polygon is generated, and the a hatch grid
        with a separation distance of `hatchSpacing` is generated to guarantee filling this bounding box region at
        any required `hatchAngle`.

        :param poly: A Shapley Polygon consisting of a polygon or path to fill with hatches
        :param hatchSpacing: The hatch spacing using to generate the truss network
        :param hatchAngle: The hatch angle to generate truss network with
        :return: The hatch lines that completely fills the geometry
        """

        # Hatch angle
        theta_h = np.radians(hatchAngle)  # 'rad'

        # Get the bounding box of the paths
        bbox = np.array(poly.bounds)

        # print('bounding box bbox', bbox)
        # Expand the bounding box
        bboxCentre = np.mean(bbox.reshape(2, 2), axis=0)

        # Calculates the diagonal length for which is the longest
        diagonal = bbox[2:] - bboxCentre
        bboxRadius = np.ceil(np.sqrt(diagonal.dot(diagonal)) / hatchSpacing) * hatchSpacing

        # Construct a square which wraps the radius
        x = np.tile(np.arange(-bboxRadius, bboxRadius, hatchSpacing, dtype=np.float32).reshape(-1, 1), (2)).flatten()
        y = np.array([-bboxRadius, bboxRadius])
        y = np.resize(y, x.shape)
        z = np.arange(0, x.shape[0] / 2, 0.5).astype(np.int64)

        coords = np.hstack([x.reshape(-1, 1),
                            y.reshape(-1, 1),
                            z.reshape(-1, 1)])

        # Create the 2D rotation matrix with an additional row, column to preserve the hatch order
        c, s = np.cos(theta_h), np.sin(theta_h)
        R = np.array([(c, -s, 0),
                      (s, c, 0),
                      (0, 0, 1.0)])

        # Apply the rotation matrix and translate to bounding box centre
        coords = np.matmul(R, coords.T)
        coords = coords.T + np.hstack([bboxCentre, 0.0])

        return coords

    def geometry(self) -> trimesh.Trimesh:
        """
        The geometry for the support structure. This resolve sthe  entire connectivity of the support truss
        meshes used by performing a boolean Union operation on the collection of meshes generated when the
        attribute :attr:`mergeMesh` option is set :code:`True`. Otherwise, the truss structure is merged
        as overlapping and non-connected meshes into a single :class:`trimesh.Trimesh` mesh..

        :return: The support geometry mesh
        """

        logging.info('Generating Mesh Geometry for GridBlock Support')
        logging.info('\tGenerating support grid slices')
        slicesX, slicesY = self.generateSupportSlices()
        logging.info('\tGenerating support border')

        if self.useSupportSkin:
            supportSkins = self.generateSupportSkins()
        else:
            supportSkins = []

        print('merging geometry', self._mergeMesh)
        # Use the Cork library to merge meshes
        if self._mergeMesh:
            logging.info('\t - Resolving Boolean Intersections betwee all support meshes')
            # Intersect the projection of the support face with the original part using the Cork Library

            isectMesh = slicesX + slicesY

            if len(isectMesh.faces) > 0:
                isectMesh = resolveIntersection(isectMesh)

            isectMesh += supportSkins
        else:
            logging.info('\t Concatenating Support Geometry Meshes Together')
            isectMesh = slicesX + slicesY + supportSkins

        # Assign a random colour to the support geometry generated
        isectMesh.visual.face_colors[:, :3] = np.random.randint(254, size=3)
        return isectMesh

    def generateSliceBoundingBoxPolygon(self, section: trimesh.path.Path2D) -> shapely.geometry.Polygon:
        """
        Generates an equivalent 2D Polygon bounding box of the support geometry volume transformed correctly into the
        local coordinate system of each grid slice by inversion of the internally stored transformation matrix in
        :class:`trimesh.path.Path2D`. This provides a consistent centroid or local origin for generating the truss
        frame across the entire support volume.

        :param section: A :class:`~trimesh.path.Path2D` cross-section consisting of a 3D transformation matrix
        :return: A 2D polygon representing the bounding box of the support geometry volume.
        """

        supportGeom = self._supportVolume
        sliceTransform = section[1]
        #bbox = np.dot(np.linalg.inv(section.metadata['to_3D'][:3, :3]), supportGeom.bounds.T).T
        bbox = np.dot(np.linalg.inv(sliceTransform[:3, :3]), supportGeom.bounds.T).T

        bx = bbox[:, 0]
        by = bbox[:, 1]
        bz = bbox[:, 2]

        a = [np.min(bx), np.max(bx)]
        b = [np.min(by), np.max(by)]

        # Create a closed polygon representing the transformed slice geometry
        bboxPoly = Polygon([[a[0], b[0]],
                            [a[0], b[1]],
                            [a[1], b[1]],
                            [a[1], b[0]],
                            [a[0], b[0]]])

        return bboxPoly

    def toothProfile(self) -> np.array:
        """
        Returns a 2D profile of a tooth used along the edge profile when generating the profile for the skin and 
        the planar truss structure.
        """

        p_a = self._supportTeethHeight
        p_b = self._supportTeethTopLength
        p_c = self._supportTeethBottomLength
        p_d = self._supportTeethBaseInterval

        # Generate a pattern
        toothPattern = [(0.0, 0.0),
                       ((p_c - p_b) / 2.0, p_a),
                       ((p_c - p_b) / 2.0 + p_b, p_a),
                       (p_c, 0.0),
                       (p_c + p_d, 0.0)]

        toothPattern = np.array(toothPattern)

        """
        Offset the pattern using the tooth height parameter with the additional upper penetration distance into the part
        """
        toothPattern[:, 1] -= p_a
        toothPattern[:, 1] += self._supportTeethUpperPenetration
        return toothPattern

    def generateSliceGeometry(self, section: trimesh.path.Path2D):
        """
        Generates a truss grid used as a 2D slice used for generating a section as part of a support structures.

        :param section: The polygon section of slice through the geometry
        :return: A Trimesh Path2D object of the truss geometry
        """

        if section[0].shape[0] == 0:
            return None

        sin_theta = getFaceZProjectionWeight(self._supportVolume, useConnectivity=False)

        topIdx = np.argwhere(sin_theta < BlockSupportGenerator._supportSkinSideTolerance)

        newIdx = section[2]
        fndIdx = np.isin(newIdx,topIdx, invert=False)
        sideIdx = np.invert(fndIdx)
        segs = section[0]#[fndIdx]
        segs = section[0]  # [fndIdx]
        unique, inverse = trimesh.grouping.unique_rows(segs.reshape(-1,2), digits=4)
        out = trimesh.path.exchange.misc.edges_to_path(edges=inverse.reshape((-1, 2)),
                                                       vertices=segs.reshape(-1,2)[unique])
        ad = trimesh.load_path(out)
        paths = ad.discrete
        slicePaths = []

        """
        Multiple polygon intersection may be generated and require processing seperately
        """

        offsetPaths = []

        for path in paths:

            coords = np.hstack([path,np.roll(path,-1,axis=0)]).reshape(-1,2,2)[:-1]

            """
            Identify the upper and lower edges of the polygon. This is guaranteed for extruded supports because the
            sides edges are always vertically orientated in (+z). Also note order is sequentially guaranteed since
            the polygons edges have been pre-sorted and correctly orientated.
            """

            idx = np.abs(np.diff(coords, axis=1).reshape(-1,2)[:,1]) > 1e-4
            splitIdx = np.split(np.arange(0, len(idx)), np.argwhere(np.diff(idx)).ravel()+1)
            partsIdx = np.argwhere(np.array([np.any(idx[split]) for split in splitIdx])) # Locate only masked edges

            newPaths = []

            upperPaths = []
            lowerPaths = []
            """ Iterate through the identified upper and lower edges found """
            for segIdx in partsIdx:

                id = int(segIdx)

                seg = np.vstack([coords[splitIdx[id]][:, 0, :],
                                 coords[splitIdx[id]][-1, 1, :]])
                ps = trimesh.path.traversal.PathSample(seg)

                """
                The bottom vertex of the path is lower than top indicates (counter-clockwise) when
                the polygon has its paths correctly sorted internally using pyclipr or Shapely. This orientation 
                gives an indication if the geometry lies at the top or the bottom of the support volume
                """
                isccw = seg[-1,1] < seg[0,1]
                dir = -1.0 if isccw else 1.0

                repPattern = self.toothProfile()

                patternLen = repPattern[-1, 0]

                numCycles = int(np.ceil(ps.length / patternLen))
                patternList = []

                for i in range(numCycles):
                    tPattern = repPattern.copy()
                    tPattern[:,0] += patternLen * i
                    patternList.append(tPattern)

                patternList = np.vstack(patternList)

                """
                Clip the interpolate positions. Trimesh PathSampler clips and repeats values, therefore only unique
                values are selected and used for generating the teeth profile
                """
                xPos, idx = np.unique(np.clip(patternList[:, 0], 0, ps.length), return_index=True)
                teethFinal = ps.sample(xPos)

                teethFinal[:,0] += dir * patternList[idx,1]

                """
                If the number of teeth profiles is beyond the length of the support edge, then exclude the use of a 
                tooth and use the original edge
                """
                if numCycles == 1:
                    teethFinal = seg

                # Prevent the generation of lower or upport support teeth
                if not self._useLowerSupportTeeth and dir > 0:
                    teethFinal = seg

                if dir > 0:
                    lowerPaths.append(teethFinal)

                # Prevent the generation of lower support teeth
                if not self._useUpperSupportTeeth and dir < 0:
                    teethFinal = seg

                if dir < 0:
                    upperPaths.append(teethFinal)

                newPaths.append(teethFinal)


            nPaths = np.vstack(newPaths)
            nPaths = np.vstack([nPaths, nPaths[0,:]])
            nPathPoly = shapely.geometry.Polygon(nPaths).buffer(1e-5)
            slicePaths.append(nPathPoly)

            """
            Add additional support to the upper and lower surfaces 
            """
            if self._supportWallThickness > 1e-5:

                if len(upperPaths) == 0 or len(lowerPaths) == 0:
                    continue

                topPolyVerts2, bottomPolyVerts2 = upperPaths[0].copy(), lowerPaths[0].copy()

                topPolyVerts2 = np.vstack([topPolyVerts2[0, :],
                                           topPolyVerts2,
                                           topPolyVerts2[-1, :],
                                           topPolyVerts2[0, :]])
                topPolyVerts2[[0, -1, -2], [0, 0, 0]] = np.min(topPolyVerts2[:, 0]) - 10.0

                bottomPolyVerts2 = np.vstack([bottomPolyVerts2[0, :],
                                              bottomPolyVerts2,
                                              bottomPolyVerts2[-1, :],
                                              bottomPolyVerts2[0, :]])
                bottomPolyVerts2[[0, -1, -2], [0, 0, 0]] = np.max(bottomPolyVerts2[:, 0]) + 10.0

                isectPolyA = shapely.geometry.Polygon(bottomPolyVerts2)  # bottom edge
                isectPolyB = shapely.geometry.Polygon(topPolyVerts2)  # top edge

                # Merge the polygon sections together and offset the boundary
                try:
                    offsetWalls = isectPolyB.buffer(1e-8).union(isectPolyA.buffer(1e-8)).buffer(self._supportWallThickness)
                    isectPolyC = offsetWalls.intersection(nPathPoly)

                    # Identify only geometry which is a polygon/multipolygon
                    if isinstance(isectPolyC, shapely.geometry.GeometryCollection):
                        fndPolys = []
                        for poly in isectPolyC.geoms:
                            if isinstance(poly, shapely.geometry.Polygon) or isinstance(poly, shapely.geometry.MultiPolygon):
                                fndPolys.append(poly)

                        offsetPaths += fndPolys
                    else:
                        offsetPaths.append(isectPolyC)
                except:
                    continue

        polys = slicePaths

        if len(polys) == 0:
            return None

        """
        Obtain the bounding box of the geometry and then transform this to local slice coordinate system. This is needed
        to ensure that the X and Y slice grids correctly align when the are eventually put together. It is achieved by
        ensuring the centroid of the support volume is used as a consistent origin in both coordinate systems.
        This transforms the support geometry based on the transformation matrix and the original bounds.
        """

        bboxPoly = self.generateSliceBoundingBoxPolygon(section)

        """
        In order to improve performance we resort to using pyclipr rather than Shapely library routines, as from
        experience this tends to perform slowly during clipping and offset operations, despite a more convenient
        API.
        """

        # Convert the shapley polygons to a path list
        convPolys = []
        for poly in polys:
            if isinstance(poly, shapely.geometry.MultiPolygon):
                convPolys += poly.geoms
            else:
                convPolys.append(poly)
        paths = path2DToPathList(convPolys)

        pc = pyclipr.ClipperOffset()
        pc.addPaths(paths, pyclipr.JoinType.Square, pyclipr.EndType.Polygon)

        if not self._generateTrussGrid:
            """
            Only generate the polygon section. This requires processing within pyclipr to process each path
            into the correct order.
            """

            # Perform the offseting operation
            outerPaths = pc.execute(1e-6)

            return outerPaths

        else:
            outerPaths = pc.execute(1e-6)

            # Offset the outer boundary to generate the interior boundary
            pc.clear()
            pc.addPaths(paths, pyclipr.JoinType.Round, pyclipr.EndType.Polygon)
            offsetPathInner = pc.execute(-self._supportBorderDistance)

        if len(offsetPathInner) < 1:
            return None

        diag = self._gridSpacing[0] * np.sin(np.deg2rad(self._trussAngle))

        # Generate the mesh grid used for the support trusses and merge the lines together
        hatchesA = self.generateMeshGrid(bboxPoly, hatchAngle=self._trussAngle, hatchSpacing=diag).reshape(-1, 2, 3)
        hatchesB = self.generateMeshGrid(bboxPoly, hatchAngle=180 - self._trussAngle, hatchSpacing=diag).reshape(-1, 2,
                                                                                                                 3)
        hatches = np.vstack([hatchesA, hatchesB])

        """
        We clip the truss hatches to remove any that are outside of the boundary in order to remove any elements which
        are not relevant to the structure in order to improve performance later
        """
        hatches2 = self.clipLines(outerPaths, hatches)


        """
        Offset the hatches to form a truss structure
        """
        pc.clear()
        pc.addPaths(hatches2, pyclipr.JoinType.Square, pyclipr.EndType.Joined)
        trussPaths = pc.execute(self._trussWidth / 2.0)

        """
        Clip or trim the Truss Paths with the exterior of the support slice boundary
        """
        pc2 = pyclipr.Clipper()
        pc2.addPaths(trussPaths, pyclipr.Subject, False)
        pc2.addPaths(outerPaths, pyclipr.Clip, False)

        if self._useSupportBorder:

            trimmedTrussPaths = pc2.execute(pyclipr.Intersection, pyclipr.FillRule.NonZero, returnOpenPaths=False, returnZ=False)

            """
            Generate the support skin
            """
            pc2.clear()
            pc2.addPaths(outerPaths, pyclipr.Subject, False)
            pc2.addPaths(offsetPathInner,  pyclipr.Clip, False)
            skinSolutionPaths = pc2.execute(pyclipr.Difference, returnOpenPaths=False, returnZ=False)

            """
            Merge all the paths together
            """
            pc2.clear()
            pc2.addPaths(trimmedTrussPaths, pyclipr.Subject, False)
            pc2.addPaths(skinSolutionPaths, pyclipr.Clip, False)
            solution = pc2.execute(pyclipr.Union)
        else:
            # Use only the truss paths. This simply exports ClipperLib PolyNode Tree

            trimmedTrussPaths = pc2.execute(pyclipr.Intersection)
            solution = trimmedTrussPaths

        if self._supportWallThickness > 1e-5:

            pc2 = pyclipr.Clipper()
            pc2.addPaths(solution, pyclipr.Clip)

            clippingPaths = []
            for mPoly in offsetPaths:
                clippingPaths += hatchingUtils.poly2Paths(mPoly)

            pc2.addPaths(clippingPaths, pyclipr.Subject, False)

            # Note open paths (lines) have to used pyclipr::Execute2 in order to perform trimming
            solution = pc2.execute2(pyclipr.Union)

        result = solution

        return result

    def generateSliceGeometryDepr(self, section):
        """
        Exists as a reference to how this can be performed using Shapely.Geometry.Polygon Objects

        :param section:
        :return:
        """

        if not section:
            return trimesh.path.Path2D()

        polys = section.polygons_closed

        if len(polys) == 0:
            return trimesh.path.Path2D()

        supportShapes = []

        for poly in polys:

            sliceBBox = poly.bounds

            if True:
                hole = self.holeGeometry()

                holes = []
                i = 1
                for x in np.arange(sliceBBox[0], sliceBBox[2], 2.25):
                    i += 1
                    for y in np.arange(sliceBBox[1], sliceBBox[3], 3):
                        y2 = y
                        if i % 2:
                            holes.append(shapely.affinity.translate(hole, x, y2))
                        else:
                            holes.append(shapely.affinity.translate(hole, x, y2 + 1.5))

                union_holes = shapely.ops.unary_union(holes)

                section_holes = poly.difference(union_holes)
            else:
                section_holes = poly.difference(poly.buffer(-self._supportBorderDistance))

            if self._useSupportBorder:
                support_border = poly.difference(poly.buffer(-self._supportBorderDistance))
                supportShape = support_border.union(section_holes)
            else:
                supportShape = section_holes

            supportShapes.append(supportShape)

        # Merge the Support Geometry
        # sliceGeometry = shapely.ops.unary_union(supportShapes)

        # print('slice geom', supportShapes)
        loadedPaths = [trimesh.path.exchange.load.load_path(path) for path in list(supportShapes)]
        sectionPath = trimesh.path.util.concatenate(loadedPaths)

        return sectionPath

    def generateSupportSkinInfill(self, myPolyVerts: np.ndarray,
                                  returnPolyNodes: Optional[bool] =False) -> Tuple[np.ndarray, np.ndarray]:
        """
        Generates a standard truss grid infill for a support border boundary  has been previously
        flattened prior to applying a 'wrapping' transformation projecting the 2D skin into the 3D support

        :param myPolyVerts: A single boundary of coordinates representing the
        :return: A mesh (vertices, faces) of the triangulated truss support order.
        """
        pc = pyclipr.ClipperOffset()
        pc.scaleFactor = int(1e4)
        # Offset the outer path to provide a clean boundary to work with
        #paths2 = np.hstack([myPolyVerts, np.arange(len(myPolyVerts)).reshape(-1, 1)])
        #paths2 = list(map(tuple, paths2))
        #clipPaths = BaseHatcher.scaleToClipper(paths2)

        """
        Offset the paths interior
        """
        pc.addPath(myPolyVerts, pyclipr.JoinType.Square, pyclipr.EndType.Polygon)

        if not self._generateTrussGrid:
            """
            Only generate the polygon section. This requires processing within pyclipr to process each path
            into the correct order.
            """
            outerPaths = pc.execute2(1e-6)

            # Process the paths and create valid path rings to form a polygon for triangulation
            exterior, interior = sortExteriorInteriorRings(outerPaths, closePolygon=True)

            # Triangulate the surface for re-mapping the mesh to the boundary
            vy, fy = pyslm.support.geometry.triangulatePolygonFromPaths(exterior[0], interior,
                                                                        triangle_args='pa{:.3f}'.format(2.0))

            return vy, fy
        else:
            outerPaths = pc.execute(1e-6)

        if False:
            import trimesh.path.traversal
            for path in outerPathsCpy:
                p = np.array(path)[:, :2]

                pd = trimesh.path.traversal.resample_path(p, step=BaseHatcher.scaleToClipper(0.5))
                outerPaths.append(pd)
                # outerPaths.append(subdivide_polygon(p, degree=1, preserve_ends=True))

        # Offset the outer boundary to make the outside
        pc.clear()

        pc.addPaths(outerPaths, pyclipr.JoinType.Round, pyclipr.EndType.Polygon)
        offsetPathInner = pc.execute(-self._supportBorderDistance)

        diag = self._gridSpacing[0] * np.sin(np.deg2rad(self._trussAngle))

        a = [np.min(myPolyVerts[:, 0]), np.max(myPolyVerts[:, 0])]
        b = [np.min(myPolyVerts[:, 1]), np.max(myPolyVerts[:, 1])]

        # Create a closed polygon representing the transformed slice geometry
        bboxPoly = Polygon([[a[0], b[0]],
                            [a[0], b[1]],
                            [a[1], b[1]],
                            [a[1], b[0]], [a[0], b[0]]])

        """
        Generate the mesh grid used for the support trusses and merge the lines together
        """
        hatchesA = self.generateMeshGrid(bboxPoly, hatchSpacing=diag, hatchAngle=self._trussAngle).reshape(-1, 2, 3)
        hatchesB = self.generateMeshGrid(bboxPoly, hatchSpacing=diag, hatchAngle=180 - self._trussAngle).reshape(-1, 2, 3)
        hatches = np.vstack([hatchesA, hatchesB])

        """
        Generate the truss by expanding the lines accordingly
        """
        pc.clear()

        pc.addPaths(hatches, pyclipr.JoinType.Square, pyclipr.EndType.Joined)
        trussPaths = pc.execute(self._trussWidth / 2.0)

        """
        Clip or trim the Truss Paths with the exterior of the support slice boundary
        """
        pc2 = pyclipr.Clipper()

        if self._useSupportBorder and len(offsetPathInner) > 0:

            pc2.addPaths(trussPaths, pyclipr.Subject)
            pc2.addPaths(outerPaths, pyclipr.Clip)

            trimmedTrussPaths = pc2.execute(pyclipr.Intersection)

            """
            Generate the support skin
            """
            pc2.clear()
            pc2.addPaths(outerPaths, pyclipr.Subject)
            pc2.addPaths(offsetPathInner, pyclipr.Clip)
            skinSolutionPaths = pc2.execute(pyclipr.Difference)

            """
            Merge all the paths together
            """
            pc2.clear()
            pc2.addPaths(trimmedTrussPaths, pyclipr.Subject)
            pc2.addPaths(skinSolutionPaths, pyclipr.Clip)

            if returnPolyNodes:
                solution = pc2.execute2(pyclipr.Union)
            else:
                solution = pc2.execute(pyclipr.Union)
        else:
            # Use only the truss paths. This simply exports ClipperLib PolyNode Tree
            pc2.addPaths(trussPaths, pyclipr.Subject)
            pc2.addPaths(outerPaths, pyclipr.Clip)

            if returnPolyNodes:
                solution = pc2.execute2(pyclipr.Intersection, pyclipr.FillRule.NonZero)
            else:
                solution = pc2.execute(pyclipr.Intersection, pyclipr.FillRule.NonZero)

        # vy, fy = geometry.triangulatePolygon(bufferPoly)

        # Triangulate the polygon - kept as a reference as an alternative

        # pyslm.visualise.plotPolygon(bufferPoly)
        # simpPolys = pyclipper.SimplifyPolygons(solution)
        # vy, fy = bufferPoly.triangulate(engine='earcut')

        # poly = Polygon(tuple(map(tuple, exterior[0])), holes=[tuple(map(tuple, ring))for ring in interior])

        # vy, fy =  pyslm.support.geometry.triangulateShapelyPolygon(poly, triangle_args='pa{:.3f}'.format(4.0))
        #vy, fy = pyslm.support.geometry.triangulatePolygonFromPaths(exterior[0], interior,
        #                                                            triangle_args='pa{:.3f}'.format(4.0))
        # vy, fy = bufferPoly.triangulate(triangle_args='pa{:.3f}'.format(4.0))
        # wvy, fy = triangulatePolygon(solution, closed=False)
        #return vy, fy

        return solution

    def generateSupportSkins(self) -> trimesh.Trimesh:
        """
        Generates the border or boundary wall of a block support structure with a truss structure for perforations for
        material removal.

        :return: A :class:`trimesh.Trimesh` object containing the mesh  of the generated support boundary
        """
        blockSupportMesh = self.supportVolume

        """
        Extract the top and bottom surfaces of the mesh that are perpendicular to the z direction
        """
        blockSupportSides = blockSupportMesh.copy()
        blockSupportSides.fix_normals()
        blockSupportSides.merge_vertices(digits_vertex=3)
        sin_theta = getFaceZProjectionWeight(blockSupportSides, useConnectivity=False)

        # First mask removes very small faces
        # Second mask isolates the outside region
        mask = blockSupportSides.area_faces > 1e-6
        mask2 = sin_theta < BlockSupportGenerator._supportSkinSideTolerance
        mask3 = np.logical_and(mask, mask2)
        blockSupportSides.update_faces(mask3)

        # Split the top and bottom surfaces to a path - guaranteed to be a manifold 2D polygon
        supportSurfCpy = blockSupportSides.split(only_watertight=False)

        supportSurf = []
        for surf in supportSurfCpy:
            if surf.area > 5:
                supportSurf.append(surf)

        if len(supportSurf) < 2:
            return []

        if len(supportSurf) > 2:

            # Uncomment below to identify issues with support generation
            #blockSupportSides.show()
            warnings.warn('Warning: number of isolated curves')
            return []

        (top, bottom) = (supportSurf[0], supportSurf[1])

        """
        Swap the curves based on their overall position in their z-position for the support structure boundary.
        Semantically this makes no difference to the generation of the support structure.
        """
        if bottom.bounds[0, 2] > top.bounds[0, 2]:
            top, bottom = bottom, top

        topPoly3D = top.outline()
        bottomPoly3D = bottom.outline()

        topPoly3D.merge_vertices(3)
        bottomPoly3D.merge_vertices(3)

        topPoly2D = topPoly3D.copy()
        bottomPoly2D = bottomPoly3D.copy()
        topPoly2D.vertices[:, 2] = 0
        bottomPoly2D.vertices[:, 2] = 0

        # Calculate the distance of the loops using the 2D (XY) Projection
        topPathLengths = [ent.length(topPoly2D.vertices) for ent in topPoly2D.entities]
        bottomPathLengths = [ent.length(bottomPoly2D.vertices) for ent in bottomPoly2D.entities]

        pairTolerance = 0.1

        pairs = []
        for i, topPath in enumerate(topPoly3D.paths):
            pathLen = sum([topPathLengths[id] for id in topPath])
            for j, bottomPath in enumerate(bottomPoly3D.paths):
                botPathLen = sum([bottomPathLengths[id] for id in bottomPath])

                if np.abs(pathLen - botPathLen) / botPathLen < pairTolerance:
                    pairs.append((i, j))

        # remove isolated edges in poly
        if len(pairs) < 1:
            print('len < 1', len(pairs), topPathLengths, bottomPathLengths)

            # Uncomment to visualise the support if there any issues with the support generation
            #blockSupportSides.show()
            return []

        topPaths = topPoly3D.paths
        bottomPaths = bottomPoly3D.paths

        if len(topPoly3D.paths) != len(bottomPoly3D.paths):
            blockSupportSides.show()
            #print('numer of paths between top and bottom do not match', len(topPoly3D.paths) , len(bottomPoly3D.paths))
            logging.warning('Number of paths between top and bottom of support structure do not match - skipping')
            return []

        boundaryMeshList = []

        for pair in pairs:

            topVerts = trimesh.path.traversal.discretize_path(topPoly3D.entities, topPoly3D.vertices, topPoly3D.paths[pair[0]])
            topPoly3Dcpy = topPoly3D.copy()
            topPoly3Dcpy.vertices[:, 2] = 0.0

            topXY = topVerts[:, :2]
            topZ = topVerts[:, 2]

            """ Ensure that the Polygon orientation is consistent in a CW fashion.  """
            if not pyclipr.orientation(topXY):
                topXY = np.flipud(topXY)
                topZ = np.flip(topZ)

            # Record the start position of the first curve for use later
            topStartPos = topXY[0]

            # topXY2 = np.vstack([topXY, topXY[0, :]])
            delta = np.diff(topXY, prepend=topXY[0, :].reshape(1, -1), axis=0)
            dist = np.sqrt(delta[:, 0] * delta[:, 0] + delta[:, 1] * delta[:, 1])

            topPolyX = np.cumsum(dist)
            topPolyY = topZ

            topPolyVerts = np.hstack([topPolyX.reshape(-1, 1), topPolyY.reshape(-1, 1)])

            """
            Complete the bottom section of the support boundary
            """
            bottomPoly3Dcpy = bottomPoly3D.copy()
            bottomPoly3Dcpy.vertices[:, 2] = 0.0

            bottomVerts = trimesh.path.traversal.discretize_path(bottomPoly3D.entities, bottomPoly3D.vertices,
                                                                 bottomPoly3D.paths[pair[1]])

            bottomXY = bottomVerts[:, :2]
            bottomZ = bottomVerts[:, 2]

            """
            The Polygon order or orientation has to be consistent with the top curve, since this is not guaranteed
            automatically by Trimesh. If the orientations are not consistent between the top and b  ottom curve both the
            (XY,Z) coordinates are flipped.
            """
            if not (pyclipr.orientation(topXY) and pyclipr.orientation(bottomXY)):
                bottomXY = np.flip(bottomXY, axis=0)
                bottomZ = np.flip(bottomZ, axis=0)

            """
            The starting pint of the curves are arbitrary set by Trimesh. We use the start point of the previous curve to
            identify the point.

            Iterate through all the points in the second curve and  find the closest point to the start index in the
            previous curve based on the distance. The coordinates (both XY, Z) are moved using numpy.roll to move the
            starting point of the curves as close to each other.
            """
            dist = bottomXY - topStartPos
            dist = np.sqrt(dist[:, 0] * dist[:, 0] + dist[:, 1] * dist[:, 1])
            startId = np.argmin(dist)
            bottomXY = np.roll(bottomXY, -startId, axis=0)
            bottomZ = np.roll(bottomZ, -startId, axis=0)

            bottomXY2 = np.vstack([bottomXY, bottomXY[0, :]])
            bottomZ = np.append(bottomZ, bottomZ[0])
            delta = np.diff(bottomXY2, prepend=bottomXY2[0, :].reshape(1, -1), axis=0)
            dist = np.sqrt(delta[:, 0] * delta[:, 0] + delta[:, 1] * delta[:, 1])

            bottomPolyX = np.cumsum(dist)
            bottomPolyY = bottomZ
            bottomPolyVerts = np.hstack([bottomPolyX.reshape(-1, 1), bottomPolyY.reshape(-1, 1)])
            bottomPolyVerts = np.flip(bottomPolyVerts, axis=0)

            """
            Form the polygon for the support boundary
            """
            if self._useLowerSupportTeeth:
                # Provide a path interpolator to resample the teeth across the profile
                ps = trimesh.path.traversal.PathSample(bottomPolyVerts)

                repPattern = self.toothProfile()

                patternLen = repPattern[-1, 0]

                numCycles = int(np.ceil(ps.length / patternLen))
                patternList = []

                # Replicate the tooth profile pattern across the entire length of the upper boundary
                for i in range(numCycles):
                    tPattern = repPattern.copy()
                    tPattern[:, 0] += patternLen * i
                    patternList.append(tPattern)

                patternList = np.vstack(patternList)

                """
                Clip the interpolate positions. Trimesh PathSampler clips and repeats values, therefore only unique
                values are selected and used for generating the teeth profile
                """
                xPos, idx = np.unique(np.clip(patternList[:, 0], 0, ps.length), return_index=True)
                teethFinalBottom = ps.sample(xPos)

                #teethFinal = ps.sample(np.clip(patternList[:, 0], 0, ps.length))
                teethFinalBottom[:, 1] += patternList[idx, 1]

            """
            The bottom vertex of the path is lower than top indicates (counter-clockwise) when
            the polygon has its paths correctly sorted internally using pyclipr or Shapely. This orientation 
            gives an indication if the geometry lies at the top or the bottom of the support volume
            """

            if self._useUpperSupportTeeth:

                # Provide a path interpolator to resample the teeth across the profile
                ps = trimesh.path.traversal.PathSample(topPolyVerts)

                repPattern = self.toothProfile()

                patternLen = repPattern[-1, 0]

                numCycles = int(np.ceil(ps.length / patternLen))
                patternList = []

                # Replicate the tooth profile pattern across the entire length of the upper boundary
                for i in range(numCycles):
                    tPattern = repPattern.copy()
                    tPattern[:, 0] += patternLen * i
                    patternList.append(tPattern)

                patternList = np.vstack(patternList)

                """
                Clip the interpolate positions. Trimesh PathSampler clips and repeats values, therefore only unique
                values are selected and used for generating the teeth profile
                """
                xPos, idx = np.unique(np.clip(patternList[:, 0], 0, ps.length), return_index=True)
                teethFinalTop = ps.sample(xPos)

                #teethFinal = ps.sample(np.clip(patternList[:, 0], 0, ps.length))
                teethFinalTop[:, 1] += patternList[idx, 1]

            vertexList = []

            if self._useUpperSupportTeeth:
                vertexList.append(teethFinalTop)
            else:
                vertexList.append(topPolyVerts)

            if self._useLowerSupportTeeth:
                vertexList.append(teethFinalBottom)
            else:
                vertexList.append(bottomPolyVerts)

            # Use the intersecting boundaries of the support volume instead
            myPolyVerts = np.vstack(vertexList)

            """ 
            Resample the boundary
            """
            myPolyVerts = trimesh.path.traversal.resample_path(myPolyVerts, step=0.25)

            """
            Add additional support to the upper and lower surfaces 
            """
            if self._supportWallThickness > 1e-5:

                infillSolution = self.generateSupportSkinInfill(myPolyVerts, returnPolyNodes=False)

                topPolyVerts2, bottomPolyVerts2 = topPolyVerts.copy(), bottomPolyVerts.copy()

                topPolyVerts2 = np.vstack([topPolyVerts2[0, :],
                                           topPolyVerts2,
                                           topPolyVerts2[-1, :],
                                           topPolyVerts2[0, :]])

                topPolyVerts2[[0, -1, -2], [1, 1, 1]] = np.max(topPolyVerts2[:, 1]) + 10.0

                bottomPolyVerts2 = np.vstack([bottomPolyVerts2[0, :],
                                              bottomPolyVerts2,
                                              bottomPolyVerts2[-1, :],
                                              bottomPolyVerts2[0, :]])

                bottomPolyVerts2[[0, -1, -2], [1, 1, 1]] = np.min(bottomPolyVerts2[:, 1]) - 10.0

                isectPolyA = shapely.geometry.Polygon(bottomPolyVerts2) # bottom edge
                isectPolyB = shapely.geometry.Polygon(topPolyVerts2) # top edge

                # Merge the polygon sections together and offset the boundary
                try:
                    offsetWalls = isectPolyB.union(isectPolyA).buffer(self._supportWallThickness)
                    isectPolyC = offsetWalls.intersection(shapely.geometry.Polygon(myPolyVerts))
                except:
                    raise Exception('error: please report bug ')

                paths = [np.array(path) for path in infillSolution]
                newPaths = [np.hstack([path, np.arange(len(path)).reshape(-1, 1)]) for path in paths]

                newPaths2 = []
                for path in newPaths:
                    newPaths2.append(np.vstack([path, path[0,:]]))

                ac = [np.array(pol) for pol in hatchingUtils.poly2Paths(isectPolyC)]

                pc = pyclipr.Clipper()
                pc.addPaths(ac, pyclipr.Clip)
                pc.addPaths(newPaths2, pyclipr.Subject)

                # Note open paths (lines) have to used pyclipr::execute2 in order to perform trimming
                result = pc.execute2(pyclipr.Union, pyclipr.FillRule.NonZero)

            else:
                result = self.generateSupportSkinInfill(myPolyVerts, returnPolyNodes=True)

            """
            Create the polygon  and triangulate using the triangle library to provide a precise controlled conformal mesh.
            """
            exterior, interior = sortExteriorInteriorRings(result, closePolygon=True)

            if len(exterior) > 1:
                raise Exception('Error: exterior count > 1')

            vy, fy = pyslm.support.geometry.triangulatePolygonFromPaths(exterior[0], interior, triangle_args='pa{:.3f}'.format(4.0))

            """
            Create the interpolation or mapping function to go from the 2D polygon to the 3D mesh for the support boundary.
            This is done based on the top most projected curve.
            """
            y1 = topXY[:, 0]
            y2 = topXY[:, 1]

            x = np.linspace(0.0, np.max(myPolyVerts[:, 0]), len(y1))
            f1 = interpolate.interp1d(topPolyX, y1, bounds_error=False, fill_value=(topXY[0, 0], topXY[-1, 0]))

            x2 = np.linspace(0.0, np.max(myPolyVerts[:, 0]), len(y2))
            f2 = interpolate.interp1d(topPolyX, y2, bounds_error=False, fill_value=(topXY[0, 1], topXY[-1, 1]))

            vy = np.hstack([vy, np.zeros([len(vy), 1])])

            """
            We subdivide and discretise the mesh further in-order to provide sufficient discretisiation of the support mesh.
            This ensures that the mesh correctly conforms to the boundary of the support block volume - especially at sharp
            apexes or corners.triangulatePo
            """
            tmpMesh = trimesh.Trimesh(vertices=vy, faces=fy, process=True, validate=True)
            tmpMesh.merge_vertices()

            vy, fy = tmpMesh.vertices, tmpMesh.faces

            """
            Subdivide the generated skin mesh to increase the mesh resolution prior to re-mapping the generated 
            2D polygon back to the conforming 3D skin 
            """
            for i in range(self._numSkinMeshSubdivideIterations):
                vy, fy = trimesh.remesh.subdivide(vy,fy)

            """
            Interpolate or map the planar 2D mesh using the existing boundary path to generate the 3D support volume
            """
            boundaryX = f1(vy[:, 0])
            boundaryY = f2(vy[:, 0])
            boundaryZ = vy[:, 1]

            verts = np.hstack([boundaryX.reshape(-1, 1), boundaryY.reshape(-1, 1), boundaryZ.reshape(-1, 1)])
            # Append a z coordinate in order to transform to mesh
            boundaryMesh = trimesh.Trimesh(vertices=verts, faces=fy, process=True)
            boundaryMeshList.append(boundaryMesh)

        return boundaryMeshList

    def generateSupportSlices(self):
        """
        Generates the XY Grid of Support truss slice meshes for generating the interior of each support.

        :return: A tuple of X,Y grid slices
        """
        xSectionMeshList = [trimesh.Trimesh()]
        ySectionMeshList = [trimesh.Trimesh()]

        sectionsX, sectionsY = self.generateGridSlices()

        # Process the Section X
        for i, sectionX in enumerate(sectionsX):

            logging.info('\tX Support generated - {:d}/{:d}'.format(i + 1, len(sectionsX)))
            section = self.generateSliceGeometry(sectionX)

            if section is None:
                continue

            if True:
                # This mode can be a faster performance version

                # poly = Polygon(tuple(map(tuple, exterior[0])), holes=[tuple(map(tuple, ring)) for ring in interior])
                # vy, fy =  pyslm.support.geometry.triangulateShapelyPolygon(poly, triangle_args='pa{:.3f}'.format(4.0))

                vx = []
                fx = []

                idx = 0
                for sect in section.children:
                    exterior, interior = sortExteriorInteriorRings(sect, closePolygon=True)
                    vertsx, facesx = pyslm.support.geometry.triangulatePolygonFromPaths(exterior[0], interior,
                                                                                        triangle_args='pa{:.3f}'.format(4.0))

                    vx.append(vertsx)
                    fx.append(facesx + idx)
                    idx += len(vertsx)

                vx = np.vstack(vx)
                fx = np.vstack(fx)

            else:
                # Triangulate the polygon
                vx, fx = triangulatePolygon(section)

            if len(vx) == 0:
                continue

            # Append a Z coordinate in order to transform to mesh
            vx = np.insert(vx, 2, values=0.0, axis=1)
            secX = trimesh.Trimesh(vertices=vx, faces=fx)

            # Transform using the original transformation matrix generated during slicing
            secX.apply_transform(sectionX.metadata['to_3D'])
            xSectionMeshList.append(secX)

        logging.info('Compounding X Grid meshes')
        # Concatenate all the truss meshes for the x-slices together in a single pass
        xSectionMesh = trimesh.util.concatenate(xSectionMeshList)

        # Process the Section Y
        for i, sectionY in enumerate(sectionsY):

            logging.info('Y Support grid slice generated - {:d}/{:d}'.format(i + 1, len(sectionsY)))

            section = self.generateSliceGeometry(sectionY)

            if section is None:
                continue

            if True:
                # This mode can be a faster performance version

                vy = []
                fy = []

                idx = 0
                for sect in section.children:

                    exterior, interior = sortExteriorInteriorRings(sect, closePolygon=True)
                    vertsy, facesy = pyslm.support.geometry.triangulatePolygonFromPaths(exterior[0], interior,
                                                                                triangle_args='pa{:.3f}'.format(4.0))
                    vy.append(vertsy)
                    fy.append(facesy + idx)
                    idx += len(vertsy)

                vy = np.vstack(vy)
                fy = np.vstack(fy)

                # poly = Polygon(tuple(map(tuple, exterior[0])), holes=[tuple(map(tuple, ring)) for ring in interior])

                # vy, fy =  pyslm.support.geometry.triangulateShapelyPolygon(poly, triangle_args='pa{:.3f}'.format(4.0))
                # vy, fy = pyslm.support.geometry.triangulate_polygon2(exterior[0], interior,
                #                                                     triangle_args='pa{:.3f}'.format(4.0))
            else:
                # Triangulate the polygon
                vy, fy = triangulatePolygon(section)

            if len(vy) == 0:
                continue

            # Append a z coordinate in order to transform to mesh
            vy = np.insert(vy, 2, values=0.0, axis=1)
            secY = trimesh.Trimesh(vertices=vy, faces=fy)

            # Transform using the original transformation matrix generated during slicing
            secY.apply_transform(sectionY.metadata['to_3D'])

            ySectionMeshList.append(secY)

        logging.info('\tCompounding Y Grid meshes')

        # Concatenate all the truss meshes for the y-slices together in a single pass
        ySectionMesh = trimesh.util.concatenate(ySectionMeshList)

        return xSectionMesh, ySectionMesh

    def generateGridSlices(self) -> Tuple[List[trimesh.path.Path2D], List[trimesh.path.Path2D]]:
        """
        Slices the support volume (:attr:`~BlockSupportBase.supportVolume`) in an grid based on :attr:`gridSpacing`.

        :return: Returns a tuple of the X and Y Grid Slice
        """

        # Obtain the bounding box for the support volume
        supportGeom = self._supportVolume

        bx = supportGeom.bounds[:, 0]
        by = supportGeom.bounds[:, 1]
        bz = supportGeom.bounds[:, 2]

        # Specify the spacing of the support grid slices
        spacingX = self._gridSpacing[0]
        spacingY = self._gridSpacing[1]

        # Obtain the section through the STL extension using Trimesh Algorithm (Shapely)
        midX = (bx[0] + bx[1]) / 2.0
        bottomX = -1.0 * np.ceil((midX - bx[0]) / spacingX) * spacingX
        topX = 1. * np.ceil((bx[1] - midX) / spacingX) * spacingX + 1e-8

        # Generate the support slices of the section
        sectionsX = supportGeom.section_multiplane(plane_origin=[midX, 0.0, 0.0],
                                                   plane_normal=[1.0, 0.0, 0.0],
                                                   heights=np.arange(bottomX, topX, spacingX))
        midY = (by[0] + by[1]) / 2.0
        bottomY = -1.0 * np.ceil((midY - by[0]) / spacingY) * spacingY
        topY = 1.0 * np.ceil((by[1] - midY) / spacingY) * spacingY + 1e-8

        sectionsY = supportGeom.section_multiplane(plane_origin=[0.0, midY, 0.0],
                                                   plane_normal=[0.0, 1.0, 0.0],
                                                   heights=np.arange(bottomY, topY, spacingY))

        return sectionsX, sectionsY


class GridBlockSupportGenerator(BlockSupportGenerator):
    """
    The GridBlockSupportGenerator class provides common methods used for generating the 'support' structures
    typically used in Metal Additive Manufacturing for block polygon regions. A truss structure is generated that
    provides more efficient scanning of supports of exposure based processes, by minimising exposure jumps.
    """

    def __init__(self):
        super().__init__()

        self._gridSpacing = [3, 3]
        self._useSupportSkin = True
        self._useSupportBorder = True
        self._useLowerSupportTeeth = True
        self._useUpperSupportTeeth = True
        self._numSkinMeshSubdivideIterations = 2

        # Support teeth parameters
        self._supportTeethHeight = 1.5  # mm
        self._supportTeethTopLength = 0.1 # mm
        self._supportTeethBottomLength = 1.5 # mm
        self._supportTeethBaseInterval = 0.2 # mm
        self._supportTeethUpperPenetration = 0.2 # mm

        self._mergeMesh = False
        self._supportBorderDistance = 3.0
        self._trussWidth = 1.0
        self._trussAngle = 45

    def __str__(self):
        return 'GridBlockSupportGenerator'

    @property
    def mergeMesh(self) -> bool:
        """
        Determines if the support truss geometry should be merged together into a connected unified mesh
        """
        return self._mergeMesh

    @mergeMesh.setter
    def mergeMesh(self, state: bool):
        self._mergeMesh = state

    @property
    def trussWidth(self) -> float:
        """
        The width of a strut in the truss grid
        """
        return self._trussWidth

    @trussWidth.setter
    def trussWidth(self, width: float):
        self._trussWidth = width

    @property
    def useSupportSkin(self) -> bool:
        """ Generates a truss support skin around the extruded boundary of the support """
        return self._useSupportSkin

    @useSupportSkin.setter
    def useSupportSkin(self, value):
        self._useSupportSkin = value

    @property
    def useSupportBorder(self):
        """ Generates a border around each truss grid """
        return self._useSupportBorder

    @useSupportBorder.setter
    def useSupportBorder(self, value):
        self._useSupportBorder = value

    @property
    def useUpperSupportTeeth(self):
        return self._useUpperSupportTeeth

    @useUpperSupportTeeth.setter
    def useUpperSupportTeeth(self, value):
        self._useUpperSupportTeeth = value

    @property
    def useLowerSupporTeeth(self):
        return self._useUpperSupportTeeth

    @useLowerSupporTeeth.setter
    def useLowerSupporTeeth(self, value):
        self._useLowerSupportTeeth = value
    @property
    def supportBorderDistance(self) -> float:
        """
        The offset used when generating a border or support skin for each truss slice in the support block.
        """
        return self._supportBorderDistance

    @supportBorderDistance.setter
    def supportBorderDistance(self, distance: float):
        self._supportBorderDistance = distance

    @property
    def trussAngle(self) -> float:
        """ The angle (degrees) used for generating the truss structures used in the support structure """
        return self._trussAngle

    @trussAngle.setter
    def trussAngle(self, angle: float):
        self._trussAngle = angle

    @property
    def gridSpacing(self) -> List[float]:
        """ The spacing of the grid truss structure within the block support """
        return self._gridSpacing

    @gridSpacing.setter
    def gridSpacing(self, spacing: List[float]):
        """ The Grid Spacing used for the support structure """
        self._gridSpacing = spacing


    @property
    def supportTeethHeight(self) -> float:
        """
        The height of the perforated support teeth
        """
        return self._supportTeethHeight

    @supportTeethHeight.setter
    def supportTeethHeight(self, teethHeight: float):
        self._supportTeethHeight = teethHeight

    @property
    def supportTeethTopLength(self):
        """
        The upper span or length of the perforated support teeth
        """
        return self._supportTeethTopLength

    @supportTeethTopLength.setter
    def supportTeethTopLength(self, topLength: float):
        self._supportTeethTopLength = topLength

    @property
    def supportTeethBottomLength(self) -> float:
        """
        The bottom span or length of the perforated support teeth
        """
        return self._supportTeethTopLength

    @supportTeethBottomLength.setter
    def supportTeethBottomLength(self, bottomLength: float):
        self._supportTeethBottomLength = bottomLength

    @property
    def supportTeethBaseInterval(self) -> float:
        return self._supportTeethBaseInterval

    @supportTeethBaseInterval.setter
    def supportTeethBaseInterval(self, baseInterval: float):
        self._supportTeethBaseInterval = baseInterval

    @property
    def supportTeethUpperPenetration(self) -> float:
        """
        Vertical (+z) penetration of the support teeth into the intersecting mesh
        """
        return self._supportTeethUpperPenetration

    @supportTeethUpperPenetration.setter
    def supportTeethUpperPenetration(self, distance: float):
        self._supportTeethUpperPenetration = distance

    def identifySupportRegions(self, part: Part, overhangAngle: float,
                               findSelfIntersectingSupport: Optional[bool] = True) -> List[GridBlockSupport]:
        """
        Extracts the overhang mesh and generates block regions given a part and target overhang angle. The algorithm
        uses a combination of boolean operations and ray intersection/projection to discriminate support regions.
        If :code:`findSelfIntersectingSupport` is to set :code:`True` (default), the algorithm will process and
        separate overhang regions that by downward projection self-intersect with the part.
        This provides more refined behavior than simply projected support material downwards into larger support
        block regions and separates an overhang surface between intersecting and non-intersecting regions.

        :param part: Part for generating support structures for
        :param overhangAngle: Overhang angle (degrees)
        :param findSelfIntersectingSupport: Generates supports that intersect with the part

        :return: A list of BlockSupports
        """
        supportBlocks = super().identifySupportRegions(part, overhangAngle, findSelfIntersectingSupport)

        gridBlocks = []

        for block in supportBlocks:
            gridBlock = GridBlockSupport(block.supportObject,
                                         block.supportVolume, block.supportSurface, block.intersectsPart)

            # Assign the GridBlock Parameters
            gridBlock.gridSpacing = self._gridSpacing
            gridBlock.numSkinMeshSubdivideIterations = self._numSkinMeshSubdivideIterations

            # Support Teeth Parameters
            gridBlock.supportTeethHeight = self._supportTeethHeight
            gridBlock.supportTeethTopLength = self._supportTeethHeight
            gridBlock.supportTeethBottomLength     = self._supportTeethBottomLength
            gridBlock.supportTeethBaseInterval     = self._supportTeethBaseInterval
            gridBlock.supportTeethUpperPenetration = self._supportTeethUpperPenetration

            # Options for generating the truss
            gridBlock.useSupportSkin        = self._useSupportSkin
            gridBlock.useSupportBorder      = self._useSupportBorder
            gridBlock.useLowerSupportTeeth  = self._useLowerSupportTeeth
            gridBlock.useUpperSupportTeeth  = self._useUpperSupportTeeth

            gridBlock.supportBorderDistance = self._supportBorderDistance
            gridBlock.trussWidth = self._trussWidth
            gridBlock.trussAngle = self._trussAngle
            gridBlock.mergeMesh  = self._mergeMesh
            gridBlock._upperSurface = block._upperSurface

            gridBlocks.append(gridBlock)

        return gridBlocks
