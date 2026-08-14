"""`generate_3d_model` AgentCore Gateway Lambda tool target.

Generates 3D parametric models (STL) from a Ground_Truth_JSON document using
CadQuery. Geometry is constructed by a builder-pattern hierarchy — a
``BaseBuilder`` subclass per part type reads dimensions from the JSON and
constructs the solid. The exported STL is uploaded to the models S3 bucket and
the model URL is returned. When the drawing lacks sufficient dimensional data,
the tool reports the missing geometric parameters instead of failing hard.
"""
