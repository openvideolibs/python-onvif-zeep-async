python-onvif-zeep-async
=======================

.. image:: https://codecov.io/gh/openvideolibs/python-onvif-zeep-async/branch/async/graph/badge.svg
    :target: https://codecov.io/gh/openvideolibs/python-onvif-zeep-async
    :alt: codecov

ONVIF Client Implementation in Python 3

Documentation
-------------
* `Source code and issue tracker <https://github.com/openvideolibs/python-onvif-zeep-async>`_
* `PyPI package <https://pypi.org/project/onvif-zeep-async/>`_

Dependencies
------------
`zeep[async] <http://docs.python-zeep.org>`_ >= 4.1.0, < 5.0.0
`httpx <https://www.python-httpx.org/>`_ >= 0.19.0, < 1.0.0

Install python-onvif-zeep-async
-------------------------------
From PyPI::

    pip install --upgrade onvif-zeep-async

From source::

    git clone https://github.com/openvideolibs/python-onvif-zeep-async.git
    cd python-onvif-zeep-async
    pip install .


Getting Started
---------------

Initialize an ONVIFCamera instance
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The WSDL files needed to talk to a camera are bundled with this package, so
you do not need to download or pass them yourself::

    import asyncio
    from onvif import ONVIFCamera

    async def main():
        mycam = ONVIFCamera('192.168.0.2', 80, 'user', 'passwd')
        await mycam.update_xaddrs()
        resp = await mycam.devicemgmt.GetHostname()
        print(f"My camera's hostname: {resp.Name}")

    asyncio.run(main())

The ``ONVIFCamera`` constructor accepts an optional ``wsdl_dir`` argument if
you need to point it at a custom WSDL directory; omit it to use the bundled
files. After ``update_xaddrs()`` the ``devicemgmt`` service is available on
the instance, exposing every operation defined in the device management WSDL.

Constructor options
~~~~~~~~~~~~~~~~~~~~

Beyond the connection details, ``ONVIFCamera`` accepts several keyword
arguments that change how it authenticates and reaches the device::

    mycam = ONVIFCamera(
        '192.168.0.2', 80, 'user', 'passwd',
        wsdl_dir='/path/to/wsdl',   # custom WSDL directory (default: bundled files)
        encrypt=True,               # WS-Security password digest (see below)
        no_cache=False,             # disable the on-disk WSDL cache
        adjust_time=False,          # tolerate cameras with a wrong clock
        nat_override=False,         # rewrite device-advertised URLs (see below)
    )

* ``encrypt`` (default ``True``) — send the WS-Security ``UsernameToken`` as a
  password *digest*. Set it to ``False`` to send the password in plain text for
  cameras that do not support digest authentication.
* ``no_cache`` (default ``False``) — skip the shared on-disk SQLite cache used
  to parse the WSDL files. Useful in read-only or ephemeral environments where
  the cache file cannot be written.
* ``adjust_time`` (default ``False``) — compensate for cameras whose clock is
  not synchronized by folding the measured time difference into the security
  token, allowing authentication to succeed. NTP on both ends is the
  recommended solution; only use this in trusted environments. It cannot be
  used on AXIS cameras, which authenticate every request.
* ``nat_override`` (default ``False``) — rewrite the host:port of URLs the
  device advertises (XAddrs, subscription addresses, snapshot URIs) to the
  host:port you passed to the constructor. Required for cameras behind NAT,
  which advertise their unreachable LAN address. Assumes a single port-forward
  to the device; RTSP stream URIs from ``GetStreamUri`` are not rewritten.

Get information from your camera
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
::

    # Get Hostname
    resp = await mycam.devicemgmt.GetHostname()
    print(f"My camera's hostname: {resp.Name}")

    # Get system date and time
    dt = await mycam.devicemgmt.GetSystemDateAndTime()
    tz = dt.TimeZone
    year = dt.UTCDateTime.Date.Year
    hour = dt.UTCDateTime.Time.Hour

Configure (Control) your camera
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

To configure your camera, there are two ways to pass parameters to service methods.

**Dict**

This is the simpler way::

    params = {'Name': 'NewHostName'}
    await device_service.SetHostname(params)

**Type Instance**

This is the recommended way. Type instance will raise an
exception if you set an invalid (or non-existent) parameter.

::

    params = mycam.devicemgmt.create_type('SetHostname')
    params.Hostname = 'NewHostName'
    await mycam.devicemgmt.SetHostname(params)

    time_params = mycam.devicemgmt.create_type('SetSystemDateAndTime')
    time_params.DateTimeType = 'Manual'
    time_params.DaylightSavings = True
    time_params.TimeZone.TZ = 'CST-8:00:00'
    time_params.UTCDateTime.Date.Year = 2014
    time_params.UTCDateTime.Date.Month = 12
    time_params.UTCDateTime.Date.Day = 3
    time_params.UTCDateTime.Time.Hour = 9
    time_params.UTCDateTime.Time.Minute = 36
    time_params.UTCDateTime.Time.Second = 11
    await mycam.devicemgmt.SetSystemDateAndTime(time_params)

Use other services
~~~~~~~~~~~~~~~~~~
ONVIF protocol has defined many services.
You can find all the services and operations `here <http://www.onvif.org/onvif/ver20/util/operationIndex.html>`_.
ONVIFCamera has support methods to create new services::

    # Create ptz service
    ptz_service = mycam.create_ptz_service()
    # Get ptz configuration
    await mycam.ptz.GetConfiguration()
    # Another way
    # await ptz_service.GetConfiguration()

Or create an unofficial service::

    xaddr = 'http://192.168.0.3:8888/onvif/yourservice'
    yourservice = mycam.create_onvif_service('service.wsdl', xaddr, 'yourservice')
    await yourservice.SomeOperation()
    # Another way
    # await mycam.yourservice.SomeOperation()

References
----------

* `ONVIF Offical Website <http://www.onvif.com>`_

* `Operations Index <http://www.onvif.org/onvif/ver20/util/operationIndex.html>`_

* `ONVIF Develop Documents <http://www.onvif.org/specs/DocMap-2.4.2.html>`_

* `Foscam Python Lib <http://github.com/quatanium/foscam-python-lib>`_
