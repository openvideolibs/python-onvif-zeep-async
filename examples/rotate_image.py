import asyncio

from onvif import ONVIFCamera


async def rotate_image_180():
    """ Rotate the image """

    # Create the media service
    mycam = ONVIFCamera("192.168.0.112", 80, "admin", "12345")
    await mycam.update_xaddrs()
    media_service = mycam.create_media_service()

    profiles = await media_service.GetProfiles()

    # Use the first profile and Profiles have at least one
    token = profiles[0].token

    # Get all video source configurations
    configurations_list = await media_service.GetVideoSourceConfigurations()

    # Use the first profile and Profiles have at least one
    video_source_configuration = configurations_list[0]

    # Enable rotate
    video_source_configuration.Extension[0].Rotate[0].Mode[0] = "OFF"

    # Create request type instance
    request = media_service.create_type("SetVideoSourceConfiguration")
    request.Configuration = video_source_configuration

    # ForcePersistence is obsolete and should always be assumed to be True
    request.ForcePersistence = True

    # Set the video source configuration
    await media_service.SetVideoSourceConfiguration(request)
    await mycam.close()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(rotate_image_180())
