TFSnippet
=========

This is a clone from haowen-xu/tfsnippet, and I have made some revisions to suit the Python version upto date. Migrate the TensorFlow code from TensorFlow 1.x to TensorFlow 2.

Installation
------------

.. code-block:: bash

    pip install git+https://github.com/Yan9564/tfsnippet.git


Modifications
-------------

**1. tfsnippet/utils/type_utils.py**

before:

.. code-block:: python

   # tfsnippet/utils/type_utils.py
    __INTEGER_TYPES = (
    six.integer_types +
    (np.integer, np.int, np.uint,
     np.int8, np.int16, np.int32, np.int64,
     np.uint8, np.uint16, np.uint32, np.uint64)

after:

.. code-block:: python

   # tfsnippet/utils/type_utils.py
    __INTEGER_TYPES = (
    six.integer_types +
    (np.integer, int, np.uint,
     np.int8, np.int16, np.int32, np.int64,
     np.uint8, np.uint16, np.uint32, np.uint64)

**2. tfsnippet/utils/type_utils.py**

before:

.. code-block:: python

   # tfsnippet/utils/type_utils.py
   __FLOATING_TYPES = (
     float,
     np.float,
     np.float16, np.float32, np.float64,)

after:

.. code-block:: python

   # tfsnippet/utils/type_utils.py
   __FLOATING_TYPES = (
     float,
     float,
     np.float16, np.float32, np.float64,)

**3. Convert the Code to TensorFlow 2.x**
